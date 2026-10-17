"""Interface Windows FCS Backup v2. Tous les appels Tkinter restent sur le fil principal."""
import json
import os
import queue
import re
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

if __package__:
    from .backup_engine import BackupEngine
else:
    from backup_engine import BackupEngine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'client_config.json')
LOG_FILE = os.path.join(BASE_DIR, 'fcs2_client.log')
LOG_MAX_LINES = 1000
LOG_TRIM_MARGIN = 100
DEFAULT_CONFIG = {
    'server_ip': '100.x.x.x', 'server_port': 5001,
    'secret_token': 'change-moi-par-un-vrai-token', 'folders': [],
    'backup_time': '02:00', 'last_backup': 'jamais', 'parallel_transfers': 2,
    'folder_labels': {},
}


def load_config():
    config = deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding='utf-8-sig') as stream:
            config.update(json.load(stream))
    if not isinstance(config['folders'], list) or not all(isinstance(p, str) and p for p in config['folders']):
        raise ValueError('Liste de dossiers invalide')
    if not isinstance(config['folder_labels'], dict) or not all(
            isinstance(path, str) and isinstance(label, str) and label
            for path, label in config['folder_labels'].items()):
        raise ValueError('Libellés de dossiers invalides')
    datetime.strptime(config['backup_time'], '%H:%M')
    if not 1 <= int(config['parallel_transfers']) <= 4:
        raise ValueError('Le nombre de transferts doit être compris entre 1 et 4')
    return config


def save_config(config):
    temporary = CONFIG_FILE + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as stream:
        json.dump(config, stream, indent=4, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, CONFIG_FILE)


def load_log_history():
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, encoding='utf-8', errors='replace') as stream:
            return [line.rstrip('\n') for line in stream.readlines()[-LOG_MAX_LINES:]]
    except OSError:
        return []


def append_log_line(line):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as stream:
            stream.write(line + '\n')
        return True
    except OSError:
        return False


def trim_log_file():
    try:
        with open(LOG_FILE, encoding='utf-8', errors='replace') as stream:
            lines = stream.readlines()[-LOG_MAX_LINES:]
        with open(LOG_FILE, 'w', encoding='utf-8') as stream:
            stream.writelines(lines)
        return len(lines)
    except OSError:
        return 0


def human_size(value):
    value = float(value)
    for unit in ('o', 'Kio', 'Mio', 'Gio', 'Tio'):
        if value < 1024 or unit == 'Tio':
            return '{:.1f} {}'.format(value, unit)
        value /= 1024


class FCSBackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title('FCS Backup — Sauvegarde')
        self.root.geometry('1080x720')
        self.root.minsize(900, 600)
        self.config = load_config()
        self.ui_queue = queue.Queue()
        self.current_engine = None
        self.tray_icon = None
        self.trash_window = None
        self.running = True
        self.quit_pending = False
        self.health_pending = False
        self.scheduled_day = None
        self._persisted_log_count = 0
        self._build_ui()
        self._load_log_history()
        self.root.after(100, self._drain_ui)
        self._start_scheduler()
        self._schedule_health_check()

    def _build_ui(self):
        pad = {'padx': 10, 'pady': 5}
        status = tk.Frame(self.root, bg='#2b2b2b')
        status.pack(fill='x')
        self.status_dot = tk.Label(status, text='●', fg='gray', bg='#2b2b2b', font=('Arial', 14))
        self.status_dot.pack(side='left', padx=8)
        self.status_label = tk.Label(status, text='Vérification...', fg='white', bg='#2b2b2b')
        self.status_label.pack(side='left')
        self.next_backup_label = tk.Label(status, fg='#aaaaaa', bg='#2b2b2b')
        self.next_backup_label.pack(side='right', padx=10)
        self.disk_label = tk.Label(self.root, text='Espace serveur : en attente', anchor='w')
        self.disk_label.pack(fill='x', padx=12, pady=2)
        self.operation_label = tk.Label(self.root, text='Prêt', anchor='w', fg='#555555', wraplength=1020)
        self.operation_label.pack(fill='x', padx=12, pady=2)

        srv = ttk.LabelFrame(self.root, text='Serveur')
        srv.pack(fill='x', **pad)
        tk.Label(srv, text='IP Tailscale :').grid(row=0, column=0, padx=5, pady=6)
        self.ip_var = tk.StringVar(value=self.config['server_ip'])
        tk.Entry(srv, textvariable=self.ip_var, width=18).grid(row=0, column=1, padx=5)
        tk.Label(srv, text='Token :').grid(row=0, column=2, padx=5)
        self.token_var = tk.StringVar(value=self.config['secret_token'])
        tk.Entry(srv, textvariable=self.token_var, width=25, show='*').grid(row=0, column=3, padx=5)
        tk.Label(srv, text='Transferts :').grid(row=0, column=4, padx=5)
        self.parallel_var = tk.StringVar(value=str(self.config['parallel_transfers']))
        ttk.Spinbox(srv, from_=1, to=4, textvariable=self.parallel_var, width=3).grid(row=0, column=5, padx=5)
        self.save_btn = ttk.Button(srv, text='Sauvegarder', command=self._save_config)
        self.save_btn.grid(row=0, column=6, padx=5)

        folders = ttk.LabelFrame(self.root, text='Dossiers à sauvegarder')
        folders.pack(fill='both', expand=True, **pad)
        self.folder_lb = tk.Listbox(folders, height=6)
        self.folder_lb.pack(side='left', fill='both', expand=True, padx=5, pady=5)
        scrollbar = ttk.Scrollbar(folders, orient='vertical', command=self.folder_lb.yview)
        scrollbar.pack(side='left', fill='y')
        self.folder_lb.config(yscrollcommand=scrollbar.set)
        buttons = tk.Frame(folders)
        buttons.pack(side='left', padx=5)
        self.add_btn = ttk.Button(buttons, text='+ Ajouter', command=self._add_folder)
        self.add_btn.pack(pady=3, fill='x')
        self.remove_btn = ttk.Button(buttons, text='− Retirer', command=self._remove_folder)
        self.remove_btn.pack(pady=3, fill='x')
        for folder in self.config['folders']:
            self.folder_lb.insert('end', self._display_text(folder))

        actions = tk.Frame(self.root)
        actions.pack(fill='x', **pad)
        self.backup_btn = ttk.Button(actions, text='▶ Sauvegarder', command=self._manual_backup)
        self.backup_btn.pack(side='left')
        self.stop_btn = ttk.Button(actions, text='⏸ Arrêter', command=self._stop_backup, state='disabled')
        self.stop_btn.pack(side='left', padx=4)
        self.reindex_btn = ttk.Button(actions, text='Réanalyser la sauvegarde', command=self._manual_reindex)
        self.reindex_btn.pack(side='left', padx=4)
        self.trash_btn = ttk.Button(actions, text='Corbeille (48 h)', command=self._show_trash)
        self.trash_btn.pack(side='left', padx=4)
        self.last_label = tk.Label(self.root, text='Dernier backup réussi : ' + self.config['last_backup'], anchor='w')
        self.last_label.pack(fill='x', padx=12)

        journal = ttk.LabelFrame(self.root, text='Journal')
        journal.pack(fill='both', expand=True, **pad)
        self.log_text = tk.Text(journal, height=9, state='disabled', bg='#1e1e1e', fg='#d4d4d4', font=('Courier', 9))
        self.log_text.pack(side='left', fill='both', expand=True, padx=5, pady=5)
        scroll = ttk.Scrollbar(journal, orient='vertical', command=self.log_text.yview)
        scroll.pack(side='left', fill='y')
        self.log_text.config(yscrollcommand=scroll.set)
        self.progress_frame = tk.Frame(self.root)
        self.progress_frame.columnconfigure(1, weight=1)
        self.progress_label = tk.Label(self.progress_frame, anchor='w', width=27)
        self.progress_label.grid(row=0, column=0, padx=10)
        self.progress_bar = ttk.Progressbar(self.progress_frame, mode='determinate')
        self.progress_bar.grid(row=0, column=1, sticky='ew', padx=10)
        self.chunk_text_frame = tk.Frame(self.progress_frame)
        self.chunk_text_frame.grid(row=1, column=0, padx=10)
        self.chunk_label = tk.Label(self.chunk_text_frame, anchor='w', wraplength=400)
        self.chunk_label.pack(anchor='w')
        self.chunk_sublabel = tk.Label(self.chunk_text_frame, anchor='w')
        self.chunk_sublabel.pack(anchor='w')
        self.chunk_bar = ttk.Progressbar(self.progress_frame, mode='determinate')
        self.chunk_bar.grid(row=1, column=1, sticky='ew', padx=10)
        self.chunk_text_frame.grid_remove()
        self.chunk_bar.grid_remove()

    def _post(self, callback, *args):
        self.ui_queue.put((callback, args))

    def _display_text(self, text):
        # Les libellés ne changent jamais les chemins utilisés par le moteur.
        replacements = {}
        for folder, label in self.config.get('folder_labels', {}).items():
            name = os.path.basename(os.path.normpath(folder))
            for prefix in {folder, os.path.normpath(folder), name}:
                if prefix:
                    replacements[prefix.casefold()] = label
        if not replacements:
            return text
        pattern = '|'.join(re.escape(prefix) for prefix in sorted(replacements, key=len, reverse=True))
        return re.sub(pattern, lambda match: replacements[match.group().casefold()], text, flags=re.IGNORECASE)

    def _drain_ui(self):
        deadline = time.monotonic() + 0.04
        while self.running and time.monotonic() < deadline:
            try:
                callback, args = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except Exception as error:
                self._append_log_ui('[{}] Erreur interface : {}'.format(datetime.now().isoformat(' ', 'seconds'), error))
        if self.running:
            self.root.after(100, self._drain_ui)

    def _load_log_history(self):
        lines = load_log_history()
        self._persisted_log_count = len(lines)
        self.log_text.config(state='normal')
        self.log_text.insert('end', ''.join(self._display_text(line) + '\n' for line in lines))
        self.log_text.see('end')
        self.log_text.config(state='disabled')

    def _log(self, message):
        line = '[{}] {}'.format(datetime.now().strftime('%Y-%m-%d %H:%M:%S'), message)
        self._post(self._append_log_ui, line)

    def _append_log_ui(self, line):
        line = self._display_text(line)
        self.log_text.config(state='normal')
        self.log_text.insert('end', line + '\n')
        if int(self.log_text.index('end-1c').split('.')[0]) > LOG_MAX_LINES + LOG_TRIM_MARGIN:
            self.log_text.delete('1.0', '{}.0'.format(LOG_TRIM_MARGIN + 1))
        self.log_text.see('end')
        self.log_text.config(state='disabled')
        if append_log_line(line):
            self._persisted_log_count += 1
            if self._persisted_log_count > LOG_MAX_LINES + LOG_TRIM_MARGIN:
                self._persisted_log_count = trim_log_file()

    def _save_config(self):
        try:
            parallel = int(self.parallel_var.get())
            if not 1 <= parallel <= 4:
                raise ValueError('Choisir entre 1 et 4 transferts')
            address, token = self.ip_var.get().strip(), self.token_var.get().strip()
            if not address or not token:
                raise ValueError('Adresse et jeton obligatoires')
            updated = deepcopy(self.config)
            updated.update(server_ip=address, secret_token=token, parallel_transfers=parallel)
            save_config(updated)
            self.config = updated
            self._log('Configuration sauvegardée')
            self._launch_health_check()
        except (ValueError, OSError) as error:
            messagebox.showerror('Configuration', str(error), parent=self.root)

    def _add_folder(self):
        if self.current_engine:
            return
        folder = filedialog.askdirectory(parent=self.root)
        if folder and folder not in self.config['folders']:
            updated = deepcopy(self.config)
            updated['folders'].append(folder)
            try:
                save_config(updated)
                self.config = updated
                self.folder_lb.insert('end', self._display_text(folder))
            except OSError as error:
                messagebox.showerror('Configuration', str(error), parent=self.root)

    def _remove_folder(self):
        if self.current_engine:
            return
        selection = self.folder_lb.curselection()
        if selection:
            updated = deepcopy(self.config)
            removed = updated['folders'].pop(selection[0])
            updated.get('folder_labels', {}).pop(removed, None)
            try:
                save_config(updated)
                self.config = updated
                self.folder_lb.delete(selection[0])
                self._log('Dossier retiré de la surveillance ; copie serveur conservée')
            except OSError as error:
                messagebox.showerror('Configuration', str(error), parent=self.root)

    def _schedule_health_check(self):
        if self.running:
            self._launch_health_check()
            self.root.after(30000, self._schedule_health_check)

    def _launch_health_check(self):
        if not self.running or self.health_pending:
            return
        self.health_pending = True
        snapshot = deepcopy(self.config)
        threading.Thread(target=self._do_health_check, args=(snapshot,), daemon=True).start()

    def _do_health_check(self, snapshot):
        engine = BackupEngine(snapshot, lambda _message: None)
        try:
            ok = engine.health_check()
            data = getattr(engine, 'health_data', {})
            self._post(self._health_done, ok, data)
        except Exception:
            self._post(self._health_done, False, {})
        finally:
            engine.close()

    def _health_done(self, ok, data):
        self.health_pending = False
        self.status_dot.config(fg='#4caf50' if ok else '#f44336')
        text = 'Serveur connecté' if ok else 'Serveur inaccessible ou incompatible'
        if ok and not data.get('ready'):
            text += ' — inventaire à réanalyser'
        self.status_label.config(text=text)
        if ok:
            self._set_disk_info(data.get('disk', {}))

    def _set_disk_info(self, data):
        if 'free' in data:
            self.disk_label.config(text='Serveur : {} libres / {} — corbeille : {}'.format(
                human_size(data['free']), human_size(data['total']), human_size(data.get('trash_bytes', 0))))

    def _set_busy(self, busy):
        for button in (self.backup_btn, self.reindex_btn, self.trash_btn, self.save_btn, self.add_btn, self.remove_btn):
            button.config(state='disabled' if busy else 'normal')
        self.stop_btn.config(state='normal' if busy else 'disabled')

    def _start_operation(self, kind, trash_id=None):
        if self.current_engine or self.quit_pending:
            return
        engine = BackupEngine(self.config, self._log)
        engine.progress_callback = self._update_progress
        engine.file_progress_callback = self._update_file_progress
        engine.chunk_progress_callback = self._update_chunk_progress
        engine.state_callback = lambda state, detail='': self._post(self._set_operation_state, state, detail)
        engine.disk_callback = lambda data: self._post(self._set_disk_info, data)
        self.current_engine = engine
        self._set_busy(True)
        self._set_operation_state('Analyse' if kind != 'restore' else 'Restauration')
        threading.Thread(target=self._run_operation, args=(kind, trash_id), daemon=True).start()

    def _manual_backup(self):
        self._start_operation('backup')

    def _manual_reindex(self):
        self._start_operation('reindex')

    def _run_operation(self, kind, trash_id=None):
        engine = self.current_engine
        success = False
        detail = ''
        try:
            if kind == 'backup':
                success = engine.run_backup()
            elif kind == 'reindex':
                success = engine.run_reindex()
            else:
                restored = 0
                for identifier in (trash_id if isinstance(trash_id, (tuple, list)) else [trash_id]):
                    if engine.stop_requested:
                        break
                    path = engine.restore(identifier)['path']
                    restored += 1
                    self._log('Restauré sur le serveur : ' + path)
                detail = '{} fichier(s) restauré(s)'.format(restored)
                self._log(detail)
                success = not engine.stop_requested
        except Exception as error:
            self._log('❌ Erreur inattendue : ' + str(error))
        finally:
            try:
                engine.close()
            except Exception as error:
                success = False
                self._log('❌ Fermeture des connexions : ' + str(error))
            state = 'Arrêté' if engine.stop_requested else ('Terminé' if success else 'Échec')
            self._post(self._finish_operation, kind, success, state, detail)

    def _finish_operation(self, kind, success, state, detail):
        self.current_engine = None
        self._hide_progress()
        self._set_busy(False)
        if success and kind == 'backup':
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.config['last_backup'] = now
            try:
                save_config(self.config)
                self.last_label.config(text='Dernier backup réussi : ' + now)
            except OSError as error:
                self._log('❌ Date du backup non enregistrée : ' + str(error))
                success, state = False, 'Échec'
        self._set_operation_state(state)
        self._set_tray_color('#4caf50' if success else '#f44336', 'FCS Backup — ' + state)
        self._launch_health_check()
        if kind == 'restore' and success and not self.quit_pending:
            self._refresh_trash()
            messagebox.showinfo('Restauration',
                detail + ' sur le serveur.' +
                '\n\nRecopie-les aussi dans leur dossier source avant le prochain backup, '
                'sinon le miroir les remettra en corbeille.', parent=self.root)
        if self.quit_pending:
            self._destroy_app()

    def _stop_backup(self):
        if self.current_engine:
            self.current_engine.stop_requested = True
            self.stop_btn.config(state='disabled')
            self._set_operation_state('Arrêt demandé')
            self._log('Arrêt demandé — attente de la requête en cours ; partiels conservés')

    def _set_operation_state(self, state, detail=''):
        color = '#b00020' if state == 'Échec' else ('#248232' if state == 'Terminé' else '#555555')
        self.operation_label.config(text=self._display_text(state + (' — ' + detail if detail else '')), fg=color)
        if state == 'Analyse serveur':
            self.progress_frame.pack(fill='x', padx=10, pady=3)
            if self.progress_bar['mode'] != 'indeterminate':
                self.progress_bar.config(mode='indeterminate')
                self.progress_bar.start(20)
            self.progress_label.config(text='Réanalyse du serveur')

    def _update_progress(self, count, total):
        self._post(self._do_update_progress, count, total, 'Analyse')

    def _update_file_progress(self, count, total):
        self._post(self._do_update_progress, count, total, 'Fichiers transférés')

    def _do_update_progress(self, count, total, label):
        if total:
            self.progress_frame.pack(fill='x', padx=10, pady=3)
            self.progress_bar.stop()
            self.progress_bar.config(mode='determinate', value=count / total * 100)
            self.progress_label.config(text='{} : {} / {}'.format(label, count, total))

    def _update_chunk_progress(self, filename, count, total):
        self._post(self._do_update_chunk_progress, filename, count, total)

    def _do_update_chunk_progress(self, filename, count, total):
        if filename is None:
            return
        self.progress_frame.pack(fill='x', padx=10, pady=3)
        self.chunk_text_frame.grid()
        self.chunk_bar.grid()
        self.chunk_label.config(text=self._display_text(filename)[-80:])
        self.chunk_sublabel.config(text='{} / {}'.format(human_size(count), human_size(total)))
        self.chunk_bar['value'] = count / total * 100 if total else 100

    def _hide_progress(self):
        self.progress_bar.stop()
        self.progress_bar.config(mode='determinate', value=0)
        self.progress_frame.pack_forget()
        self.chunk_bar.grid_remove()
        self.chunk_text_frame.grid_remove()

    def _show_trash(self):
        if self.current_engine:
            return
        if self.trash_window and self.trash_window.winfo_exists():
            self.trash_window.lift()
            return
        window = self.trash_window = tk.Toplevel(self.root)
        window.title('Corbeille serveur — conservation 48 heures')
        window.geometry('950x400')
        tk.Label(window, text='Restauration sur le serveur. Récupérer ensuite le fichier dans sa source avant le prochain miroir.',
                 wraplength=900).pack(padx=10, pady=8)
        table = tk.Frame(window)
        table.pack(fill='both', expand=True, padx=10, pady=5)
        self.trash_tree = ttk.Treeview(table, columns=('path', 'size', 'deleted', 'expires'), show='headings')
        for name, title, width in (('path', 'Fichier', 450), ('size', 'Taille', 90),
                                   ('deleted', 'Suppression', 150), ('expires', 'Expiration', 150)):
            self.trash_tree.heading(name, text=title)
            self.trash_tree.column(name, width=width)
        self.trash_tree.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(table, orient='vertical', command=self.trash_tree.yview)
        scrollbar.pack(side='right', fill='y')
        self.trash_tree.config(yscrollcommand=scrollbar.set)
        bar = tk.Frame(window)
        bar.pack(pady=8)
        ttk.Button(bar, text='Actualiser', command=self._refresh_trash).pack(side='left', padx=5)
        ttk.Button(bar, text='Restaurer la sélection sur le serveur', command=self._restore_selected).pack(side='left', padx=5)
        self._refresh_trash()

    def _refresh_trash(self):
        if not self.trash_window or not self.trash_window.winfo_exists():
            return
        snapshot = deepcopy(self.config)
        window = self.trash_window

        def worker():
            engine = BackupEngine(snapshot, self._log)
            try:
                self._post(self._populate_trash, window, engine.get_trash())
            except Exception as error:
                self._log('❌ Lecture de la corbeille : ' + str(error))
            finally:
                engine.close()
        threading.Thread(target=worker, daemon=True).start()

    def _populate_trash(self, window, entries):
        if not window.winfo_exists() or window is not self.trash_window:
            return
        for item in self.trash_tree.get_children():
            self.trash_tree.delete(item)
        for entry in entries:
            dates = [datetime.fromtimestamp(entry[key]).strftime('%Y-%m-%d %H:%M')
                     for key in ('deleted_at', 'expires_at')]
            self.trash_tree.insert('', 'end', iid=entry['id'], values=(self._display_text(entry['path']), human_size(entry['size']), *dates))

    def _restore_selected(self):
        selected = self.trash_tree.selection()
        if selected:
            self._start_operation('restore', selected)

    def _start_scheduler(self):
        self._scheduler_tick(check=False)

    def _scheduler_tick(self, check=True):
        if not self.running:
            return
        now = datetime.now()
        hour, minute = map(int, self.config['backup_time'].split(':'))
        if check and (now.hour, now.minute) == (hour, minute) and self.scheduled_day != now.date():
            self.scheduled_day = now.date()
            if not self.current_engine:
                self._manual_backup()
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        self.next_backup_label.config(text='Prochain backup : ' + next_run.strftime('%Y-%m-%d %H:%M'))
        self.root.after(60000, self._scheduler_tick)

    def on_close(self):
        if self.tray_icon:
            self.root.withdraw()
        else:
            self._quit_app()

    def _quit_app(self):
        if self.current_engine:
            self.quit_pending = True
            self._stop_backup()
        else:
            self._destroy_app()

    def _destroy_app(self):
        self.running = False
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()

    def _set_tray_color(self, color, tooltip=None):
        if not self.tray_icon:
            return
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (64, 64), '#2b2b2b')
        ImageDraw.Draw(image).ellipse([16, 16, 48, 48], fill=color)
        self.tray_icon.icon = image
        if tooltip:
            self.tray_icon.title = tooltip

    def _setup_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
            image = Image.new('RGB', (64, 64), '#2b2b2b')
            ImageDraw.Draw(image).ellipse([16, 16, 48, 48], fill='#888888')
            menu = pystray.Menu(
                pystray.MenuItem('Ouvrir FCS Backup', lambda *_args: self._post(self._show_window), default=True),
                pystray.MenuItem('Quitter', lambda *_args: self._post(self._quit_app)))
            self.tray_icon = pystray.Icon('FCS Backup', image, 'FCS Backup — Prêt', menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except ImportError:
            self._log('Icône de notification indisponible : installer les dépendances client')
            self.root.deiconify()


def main():
    root = tk.Tk()
    root.withdraw()
    try:
        app = FCSBackupApp(root)
    except Exception as error:
        messagebox.showerror('FCS Backup — Démarrage impossible', str(error), parent=root)
        root.destroy()
        return
    root.protocol('WM_DELETE_WINDOW', app.on_close)
    app._setup_tray()
    root.mainloop()


if __name__ == '__main__':
    main()
