import pathlib
import sys

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QRadioButton,
    QScrollArea,
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from PyQt6.QtWidgets import QTabWidget

from korok_engine import KorokEngine, register_bcml_protocol
from korok_instances import InstanceManager
from korok_tabs import InstancesTab, SaveEditorTab


def display_name_for(file_path: str) -> str:
    p = pathlib.Path(file_path)
    return (p.name if p.is_dir() else p.stem).replace("_", " ")


class UkmmOptionsDialog(QDialog):
    """Lets the user pick a UKMM mod's install options (defaults preselected)."""

    def __init__(self, mod_name: str, groups: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Options for {mod_name}")
        self.resize(520, 480)
        outer = QVBoxLayout(self)
        outer.addWidget(QLabel(f"'{mod_name}' has install options. Pick the ones you want:"))
        area = QScrollArea()
        area.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        self._widgets = []          # (option path, button)
        for g in groups:
            box = QGroupBox(g["name"] + ("" if g["multiple"] else "  (pick one)"))
            bl = QVBoxLayout(box)
            if g.get("description"):
                d = QLabel(g["description"])
                d.setWordWrap(True)
                bl.addWidget(d)
            group = None if g["multiple"] else QButtonGroup(box)
            if group is not None and not g["required"]:
                none = QRadioButton("None")
                none.setChecked(not g["defaults"])
                group.addButton(none)
                bl.addWidget(none)
            for o in g["options"]:
                b = QCheckBox(o["name"]) if g["multiple"] else QRadioButton(o["name"])
                b.setChecked(o["path"] in g["defaults"])
                if o.get("description"):
                    b.setToolTip(o["description"])
                if group is not None:
                    group.addButton(b)
                bl.addWidget(b)
                self._widgets.append((o["path"], b))
            lay.addWidget(box)
        lay.addStretch(1)
        area.setWidget(inner)
        outer.addWidget(area, stretch=1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def chosen(self) -> list:
        return [path for path, b in self._widgets if b.isChecked()]


class InstallWorker(QThread):
    """Imports every queued item (a file or a 1-click URL), then rebuilds the
    pack ONCE.  With an empty queue it just rebuilds (Apply / Force rebuild)."""

    progress = pyqtSignal(int, str)          # percent, message
    item_status = pyqtSignal(int, str)       # queue index, status text
    finished_all = pyqtSignal(list, str)     # results, rebuild error

    def __init__(self, engine: KorokEngine, items: list, force: bool = False):
        super().__init__()
        self.engine = engine
        self.items = items          # [("file", path) | ("url", url)]
        self.force = force

    def run(self):
        n = len(self.items)
        import_share = 50 if n else 0
        results = []  # (name, warnings, error)

        for i, (kind, value) in enumerate(self.items):
            label = display_name_for(value) if kind == "file" else "1-Click download"
            self.item_status.emit(i, "Installing...")
            self.progress.emit(int(i / n * import_share), f"Importing {label} ({i + 1}/{n})")
            try:
                if kind == "file":
                    name = display_name_for(value)
                    warnings = self.engine.import_mod_archive(value, name)
                    name = self.engine.last_imported_name or name
                else:
                    name, warnings = self.engine.handle_one_click_url(value)
                results.append((name, warnings, None))
                self.item_status.emit(i, "Done (with warnings)" if warnings else "Done")
            except Exception as e:
                results.append((label, [], str(e)))
                self.item_status.emit(i, "Failed")

        def rebuild_cb(frac: float, text: str):
            self.progress.emit(int(import_share + frac * (100 - import_share)), text)

        build_error = ""
        if n == 0 or any(err is None for _, _, err in results):
            try:
                self.engine.rebuild_consolidated_pack(progress_cb=rebuild_cb, force=self.force)
            except Exception as e:
                build_error = str(e)
        self.progress.emit(100, "Finished")
        self.finished_all.emit(results, build_error)


class KorokUI(QMainWindow):

    PENDING_TEXT = "Apply to Cemu (Rebuild Pack) - changes pending"
    NORMAL_TEXT = "Apply to Cemu (Rebuild Pack)"

    def __init__(self, engine: KorokEngine):
        super().__init__()
        self.engine = engine
        self.worker = None
        self.init_ui()

    # ------------------------------------------------------------------ UI
    def init_ui(self):
        self.setWindowTitle("Korok — Everything Breath of the Wild")
        self.resize(1020, 720)

        main_widget = QWidget()
        outer = QVBoxLayout()
        layout = QVBoxLayout()   # contents of the Mods tab

        self.instance_mgr = InstanceManager(self.engine)
        self.instances_tab = InstancesTab(self.engine, self.instance_mgr)
        self.editor_tab = SaveEditorTab(self.engine, self.instance_mgr)
        outer.addWidget(self.instances_tab.launch_box)

        # --- Cemu Directory Selection Bar ---
        cemu_layout = QHBoxLayout()
        cemu_label = QLabel("Cemu Folder:")
        self.cemu_input = QLineEdit(str(self.engine.cemu_dir))
        self.cemu_input.editingFinished.connect(self.on_path_edited)
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self.browse_cemu)
        cemu_layout.addWidget(self.cemu_input)
        cemu_layout.addWidget(self.browse_btn)
        # Cemu folder lives in the Cemu box, between Cemu.exe and Game
        self.instances_tab.launch_form.insertRow(0, "Cemu folder", cemu_layout)

        self.instance_label = QLabel()
        f = self.instance_label.font(); f.setBold(True); self.instance_label.setFont(f)
        outer.addWidget(self.instance_label)
        self.instances_tab.refreshed.connect(self.update_active_label)
        self.update_active_label()


        # --- Left: playlists tree ---
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Playlists  (click one to switch)"))
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemClicked.connect(self.on_tree_clicked)
        left_layout.addWidget(self.tree, stretch=1)

        pl_buttons = QHBoxLayout()
        self.new_pl_btn = QPushButton("New...")
        self.ren_pl_btn = QPushButton("Rename...")
        self.del_pl_btn = QPushButton("Delete")
        self.new_pl_btn.clicked.connect(self.new_playlist)
        self.ren_pl_btn.clicked.connect(self.rename_playlist)
        self.del_pl_btn.clicked.connect(self.delete_playlist)
        for b in (self.new_pl_btn, self.ren_pl_btn, self.del_pl_btn):
            pl_buttons.addWidget(b)
        left_layout.addLayout(pl_buttons)

        self.auto_apply_chk = QCheckBox("Apply automatically when switching playlists")
        self.auto_apply_chk.setChecked(bool(self.engine.state.get("auto_apply", False)))
        self.auto_apply_chk.toggled.connect(self.on_auto_apply_toggled)
        left_layout.addWidget(self.auto_apply_chk)

        # --- Right: mod table for the active playlist ---
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.active_label = QLabel()
        right_layout.addWidget(self.active_label)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["On", "#", "Mod", "Notes", "Library"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self.on_item_changed)
        right_layout.addWidget(self.table, stretch=1)

        move_row = QHBoxLayout()
        self.up_btn = QPushButton("Move Up")
        self.down_btn = QPushButton("Move Down")
        self.up_btn.clicked.connect(lambda: self.move_selected(-1))
        self.down_btn.clicked.connect(lambda: self.move_selected(1))
        move_row.addWidget(self.up_btn)
        move_row.addWidget(self.down_btn)
        move_row.addStretch(1)
        move_row.addWidget(QLabel("Load order: mods lower in the list win when two change the same file."))
        right_layout.addLayout(move_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([260, 700])
        layout.addWidget(splitter, stretch=1)

        # --- Install queue + progress (hidden until used) ---
        self.queue_label = QLabel("Install queue:")
        self.queue_list = QListWidget()
        self.queue_list.setMaximumHeight(110)
        self.progress_label = QLabel("")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        for w in (self.queue_label, self.queue_list, self.progress_label, self.progress_bar):
            w.setVisible(False)
            layout.addWidget(w)

        # --- Action Buttons ---
        btn_layout = QHBoxLayout()
        self.import_btn = QPushButton("Import Mods (.bnp / .zip)...")
        self.import_btn.clicked.connect(self.import_mod_files)
        self.import_dir_btn = QPushButton("Import Folder...")
        self.import_dir_btn.setToolTip(
            "Import an already-unpacked graphic pack: a folder containing 'content' and/or 'aoc'\n"
            "(for example the folder UKMM deploys to). The folder itself is never changed.")
        self.import_dir_btn.clicked.connect(self.import_mod_folder)
        self.apply_btn = QPushButton(self.NORMAL_TEXT)
        self.apply_btn.clicked.connect(self.apply_mods)
        self.force_btn = QPushButton("Force full rebuild")
        self.force_btn.setToolTip("Rebuild everything even if nothing changed (ignores the 'up to date' check).")
        self.force_btn.clicked.connect(self.force_rebuild)
        btn_layout.addWidget(self.import_btn)
        btn_layout.addWidget(self.import_dir_btn)
        btn_layout.addWidget(self.apply_btn, stretch=2)
        btn_layout.addWidget(self.force_btn)
        layout.addLayout(btn_layout)

        mods_page = QWidget()
        mods_page.setLayout(layout)
        self.instances_tab.changed.connect(self.refresh_all)
        self.instances_tab.busy.connect(self.on_instance_busy)
        self.main_tabs = QTabWidget()
        self.main_tabs.addTab(mods_page, "Mods")
        self.main_tabs.addTab(self.instances_tab, "Instances")
        self.main_tabs.addTab(self.editor_tab, "Save Editor")
        self.main_tabs.currentChanged.connect(self.on_tab_changed)
        outer.addWidget(self.main_tabs, stretch=1)

        main_widget.setLayout(outer)
        self.setCentralWidget(main_widget)

        self.refresh_all()

    # ---------------------------------------------------------- settings
    def update_active_label(self):
        a = self.instance_mgr.active()
        if a:
            d = self.instance_mgr.info(a)
            pl = d.get("playlist")
            self.instance_label.setText(f"Active instance: {d['name']}" + (f"   (playlist: {pl})" if pl else ""))
        else:
            self.instance_label.setText("Active instance: none")

    def on_instance_busy(self, busy: bool, text: str):
        self.main_tabs.setEnabled(not busy)
        self.statusBar().showMessage(text if busy else "", 0 if busy else 1)

    def on_tab_changed(self, idx):
        if idx == 1:
            self.instances_tab.refresh()
        elif idx == 2:
            self.editor_tab.rescan()

    def on_path_edited(self):
        text = self.cemu_input.text().strip().strip('"')
        if text:
            self.engine.set_cemu_dir(text)
            self.instances_tab.autodetect()
            self.update_pending()

    def browse_cemu(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Cemu Directory")
        if directory:
            self.cemu_input.setText(directory)
            self.engine.set_cemu_dir(directory)
            self.instances_tab.autodetect()
            self.refresh_all()

    def on_auto_apply_toggled(self, checked: bool):
        self.engine.state["auto_apply"] = bool(checked)
        self.engine._save_state()

    # ------------------------------------------------------ refreshing
    def refresh_all(self):
        self.update_active_label()
        self.instances_tab.refresh()
        self.refresh_tree()
        self.refresh_mods()
        self.update_pending()

    def update_pending(self):
        try:
            pending = self.engine.pending_changes()
        except Exception:
            pending = True
        self.apply_btn.setText(self.PENDING_TEXT if pending else self.NORMAL_TEXT)

    def refresh_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        active = self.engine.active_profile
        for name in self.engine.profile_names():
            top = QTreeWidgetItem([name + ("   (active)" if name == active else "")])
            top.setData(0, Qt.ItemDataRole.UserRole, name)
            f = QFont()
            f.setBold(name == active)
            top.setFont(0, f)
            mods = self.engine.profile_mods(name)
            for m in mods:
                child = QTreeWidgetItem([m])
                child.setData(0, Qt.ItemDataRole.UserRole, name)
                child.setData(0, Qt.ItemDataRole.UserRole + 1, m)
                top.addChild(child)
            if not mods:
                empty = QTreeWidgetItem(["(empty)"])
                empty.setFlags(Qt.ItemFlag.NoItemFlags)
                top.addChild(empty)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
        self.tree.blockSignals(False)

    def refresh_mods(self, select: str = ""):
        ordered = self.engine.ordered_mods()
        self.active_label.setText(f"Playlist: <b>{self.engine.active_profile}</b>")
        self.table.blockSignals(True)
        self.table.setRowCount(len(ordered))
        select_row = -1

        for row, mod_name in enumerate(ordered):
            info = self.engine.get_mod_info(mod_name)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if info["enabled"] else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, mod_name)
            self.table.setItem(row, 0, chk)

            num = QTableWidgetItem(str(row + 1))
            num.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 1, num)

            name_item = QTableWidgetItem(mod_name)
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if not info["enabled"]:
                name_item.setForeground(Qt.GlobalColor.gray)
            self.table.setItem(row, 2, name_item)

            note = ""
            if info["needs_merge"]:
                note = "Needs merge logs Korok can't apply (" + ", ".join(info["needs_merge"][:3]) + ")"
            note_item = QTableWidgetItem(note)
            note_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            note_item.setToolTip(note)
            self.table.setItem(row, 3, note_item)

            remove_btn = QPushButton("Delete")
            remove_btn.setToolTip("Delete this mod from the library (removes it from every playlist).")
            remove_btn.clicked.connect(lambda _checked, m=mod_name: self.delete_mod(m))
            self.table.setCellWidget(row, 4, remove_btn)

            if mod_name == select:
                select_row = row
        self.table.blockSignals(False)
        if select_row >= 0:
            self.table.selectRow(select_row)

    # ---------------------------------------------------- playlists
    def on_tree_clicked(self, item, _column):
        profile = item.data(0, Qt.ItemDataRole.UserRole)
        if not profile:
            return
        mod = item.data(0, Qt.ItemDataRole.UserRole + 1) or ""
        if self.worker is None and profile != self.engine.active_profile:
            self.engine.set_active_profile(profile)
            self.instance_mgr.follow_playlist(profile)
            self.refresh_all()
            if self.engine.state.get("auto_apply", False):
                self.apply_mods()
        if mod:
            self.refresh_mods(select=mod)

    def _ask_name(self, title: str, default: str = ""):
        text, ok = QInputDialog.getText(self, title, "Playlist name:", text=default)
        return text.strip() if ok and text.strip() else None

    def new_playlist(self):
        if self.worker is not None:
            return
        name = self._ask_name("New playlist")
        if not name:
            return
        copy = QMessageBox.question(
            self, "New playlist",
            "Start with the mods of the current playlist?\n(No = start empty)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes
        try:
            self.engine.create_profile(name, copy_from=self.engine.active_profile if copy else None)
            self.engine.set_active_profile(name)
            self.instance_mgr.follow_playlist(name)
        except ValueError as e:
            QMessageBox.warning(self, "Playlist", str(e))
        self.refresh_all()

    def rename_playlist(self):
        if self.worker is not None:
            return
        old = self.engine.active_profile
        name = self._ask_name("Rename playlist", old)
        if not name or name == old:
            return
        try:
            self.engine.rename_profile(old, name)
            self.instance_mgr.rename_playlist_refs(old, name)
        except ValueError as e:
            QMessageBox.warning(self, "Playlist", str(e))
        self.refresh_all()

    def delete_playlist(self):
        if self.worker is not None:
            return
        name = self.engine.active_profile
        if QMessageBox.question(
            self, "Delete playlist",
            f"Delete the playlist '{name}'?\n(The mods themselves stay in your library.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.engine.delete_profile(name)
        except ValueError as e:
            QMessageBox.warning(self, "Playlist", str(e))
        self.refresh_all()

    # ---------------------------------------------------- mod list
    def on_item_changed(self, item):
        if item.column() != 0:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if name:
            self.engine.set_mod_enabled(name, item.checkState() == Qt.CheckState.Checked)
            self.refresh_tree()
            self.refresh_mods(select=name)
            self.update_pending()

    def selected_mod(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 2)
        return item.text() if item else ""

    def move_selected(self, delta: int):
        if self.worker is not None:
            return
        name = self.selected_mod()
        if name and self.engine.move_mod(name, delta):
            self.refresh_tree()
            self.refresh_mods(select=name)
            self.update_pending()

    def delete_mod(self, mod_name: str):
        if self.worker is not None:
            return
        if QMessageBox.question(
            self, "Delete mod",
            f"Delete '{mod_name}' from your library?\nIt will be removed from every playlist.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.engine.delete_mod(mod_name)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete mod: {e}")
            return
        self.refresh_all()
        self.statusBar().showMessage(
            f"Deleted '{mod_name}'. Click Apply when you're ready to update the pack.", 8000)

    # ---------------------------------------------------- busy / jobs
    def set_busy(self, busy: bool):
        for w in (self.import_btn, self.import_dir_btn, self.apply_btn, self.force_btn, self.browse_btn, self.cemu_input,
                  self.table, self.tree, self.new_pl_btn, self.ren_pl_btn,
                  self.del_pl_btn, self.up_btn, self.down_btn, self.auto_apply_chk):
            w.setEnabled(not busy)
        self.progress_bar.setVisible(busy or self.progress_bar.isVisible())
        self.progress_label.setVisible(busy or self.progress_label.isVisible())

    def start_job(self, items: list, force: bool = False):
        """items: [("file", path) | ("url", url)]; empty = just rebuild."""
        if self.worker is not None:
            return
        self.queue_list.clear()
        has_queue = bool(items)
        for kind, value in items:
            label = display_name_for(value) if kind == "file" else "1-Click download"
            self.queue_list.addItem(f"{label} — Queued")
        self.queue_label.setVisible(has_queue)
        self.queue_list.setVisible(has_queue)

        self.progress_bar.setValue(0)
        self.progress_label.setText("Starting...")
        self.progress_bar.setVisible(True)
        self.progress_label.setVisible(True)
        self.set_busy(True)

        self.worker = InstallWorker(self.engine, items, force=force)
        self.worker.progress.connect(self.on_progress)
        self.worker.item_status.connect(self.on_item_status)
        self.worker.finished_all.connect(self.on_job_finished)
        self.worker.start()

    def on_progress(self, percent: int, text: str):
        self.progress_bar.setValue(percent)
        self.progress_label.setText(text)

    def on_item_status(self, index: int, status: str):
        item = self.queue_list.item(index)
        if item:
            name = item.text().split(" — ")[0]
            item.setText(f"{name} — {status}")
            self.queue_list.scrollToItem(item)

    def on_job_finished(self, results: list, build_error: str):
        self.worker.wait()
        self.worker = None
        self.set_busy(False)
        self.refresh_all()

        ok = [r for r in results if r[2] is None]
        failed = [r for r in results if r[2] is not None]
        lines = []
        if results:
            lines.append(f"Installed {len(ok)} of {len(results)} mod(s).")
        elif self.engine.last_build_skipped:
            lines.append("Nothing to do.")
        else:
            lines.append("Korok Mods package successfully updated and applied to Cemu!")
        for name, _, err in failed:
            lines.append(f"\nFailed: {name}\n  {err}")
        for name, warnings, _ in ok:
            for w in warnings:
                lines.append(f"\nWarning ({name}): {w}")
        if build_error:
            lines.append(f"\nPack rebuild failed: {build_error}")
        elif self.engine.last_rstb_report:
            lines.append("\n" + self.engine.last_rstb_report)
        if self.engine.last_merge_report and not build_error:
            lines.append("\n" + self.engine.last_merge_report)
        if self.engine.last_build_summary and not build_error:
            lines.append("\n" + self.engine.last_build_summary)
        text = "\n".join(lines)

        has_problems = "problems" in self.engine.last_merge_report
        if build_error or (results and not ok):
            QMessageBox.critical(self, "Error", text)
        elif failed or any(w for _, w, _ in ok) or has_problems:
            QMessageBox.warning(self, "Finished with warnings", text)
        else:
            QMessageBox.information(self, "Success", text)

        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)

    # ---------------------------------------------------- actions
    def apply_mods(self):
        if not self.engine.cemu_dir.exists():
            QMessageBox.warning(self, "Error", "Please select a valid Cemu directory first.")
            return
        self.start_job([])

    def force_rebuild(self):
        if not self.engine.cemu_dir.exists():
            QMessageBox.warning(self, "Error", "Please select a valid Cemu directory first.")
            return
        self.start_job([], force=True)

    def import_mod_files(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Mod Archives (hold Ctrl/Shift to select several)",
            "",
            "BotW Mod Files (*.bnp *.zip *.7z);;BNP Files (*.bnp);;Zip Files (*.zip);;7-Zip Files (*.7z)",
        )
        if file_paths:
            if any(self.engine.is_ukmm_archive(p) for p in file_paths) and \
                    not self.engine.native_ukmm_available() and not self.ensure_ukmm():
                file_paths = [p for p in file_paths if not self.engine.is_ukmm_archive(p)]
                if not file_paths:
                    return
            keep = []
            for p in file_paths:
                if self.engine.is_ukmm_archive(p):
                    groups = self.engine.ukmm_option_groups(p)
                    if groups:
                        dlg = UkmmOptionsDialog(display_name_for(p), groups, self)
                        if dlg.exec() != QDialog.DialogCode.Accepted:
                            continue          # cancelled: don't import this one
                        self.engine.ukmm_option_choices[str(pathlib.Path(p))] = dlg.chosen()
                keep.append(p)
            if keep:
                self.start_job([("file", p) for p in keep])

    def ensure_ukmm(self) -> bool:
        """UKMM packages are converted by running UKMM; make sure we know where ukmm.exe is."""
        if self.engine.ukmm_exe() is not None:
            return True
        QMessageBox.information(
            self, "UKMM needed",
            "This is a UKMM mod package. Its files are stored in UKMM's own format. Korok can "
            "read them itself once its extra packages are installed (pip install -r "
            "requirements.txt); otherwise it runs UKMM in the background to convert it (your own "
            "UKMM mods and settings are not touched).\n\nUKMM must be set up once for Wii U "
            "(game dump folders). Point Korok at ukmm.exe next.")
        exe, _ = QFileDialog.getOpenFileName(self, "Locate ukmm.exe", "", "UKMM (ukmm.exe ukmm);;All files (*)")
        if not exe:
            return False
        self.engine.set_ukmm_exe(exe)
        return True

    def import_mod_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select a graphic pack folder (contains 'content' and/or 'aoc')")
        if folder:
            if self.engine.is_ukmm_archive(folder) and not self.ensure_ukmm():
                return
            self.start_job([("file", folder)])

    def closeEvent(self, event):
        if self.worker is not None:
            QMessageBox.information(
                self, "Busy", "Korok is still working. Please wait for it to finish."
            )
            event.ignore()
        else:
            self.instances_tab.shutdown()
            event.accept()


if __name__ == "__main__":
    # Needed so the background merge workers (spawned processes) behave when
    # this file is re-imported by them.
    import multiprocessing
    multiprocessing.freeze_support()

    app = QApplication(sys.argv)

    register_bcml_protocol()

    engine = KorokEngine()
    window = KorokUI(engine)
    window.show()

    # 1-click link (bcml:/gb:): download + import in the background queue.
    if len(sys.argv) > 1 and sys.argv[1].startswith(("bcml:", "gb:")):
        url = sys.argv[1]
        QTimer.singleShot(0, lambda: window.start_job([("url", url)]))

    sys.exit(app.exec())
