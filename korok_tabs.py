"""Save Manager tab and Save Editor tab for korok."""
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

import korok_instances as ki
import korok_save as ks


class RebuildWorker(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(str)       # error text, "" on success

    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    def run(self):
        try:
            self.engine.rebuild_consolidated_pack(
                progress_cb=lambda frac, text: self.progress.emit(f"{int(frac * 100)}%  {text}"))
            self.done.emit("")
        except Exception as e:
            self.done.emit(str(e))


NO_PLAYLIST = "(none - leave my mods as they are)"


class CemuWatcher(QThread):
    """Waits for a launched Cemu to exit, then tells the UI to save progress back."""
    closed = pyqtSignal()

    def __init__(self, proc):
        super().__init__()
        self.proc = proc

    def run(self):
        try:
            self.proc.wait()
        except Exception:
            pass
        self.closed.emit()


# ===================================================================== #
class InstancesTab(QWidget):
    changed = pyqtSignal()      # instance/playlist changed -> Mods tab should refresh
    refreshed = pyqtSignal()
    busy = pyqtSignal(bool, str)  # lock the window while the pack rebuilds

    def __init__(self, engine, mgr, parent=None):
        super().__init__(parent)
        self.engine, self.mgr = engine, mgr
        self.watcher = None
        lay = QVBoxLayout(self)

        intro = QLabel(
            "A save slot is its own save file, optionally with a mod profile that is applied for you on switch. Switching swaps the save "
            "Cemu sees (your game files stay shared). Close Cemu before switching.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Active", "Save slot", "Last played"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.table, stretch=1)

        row1 = QHBoxLayout()
        self.switch_btn = QPushButton("Switch save slot")
        self.save_btn = QPushButton("Save progress now")
        self.save_btn.setToolTip("Copy the live Cemu save into the active save slot.")
        for b in (self.switch_btn, self.save_btn):
            row1.addWidget(b)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.new_btn = QPushButton("New...")
        self.ren_btn = QPushButton("Rename...")
        self.pl_btn = QPushButton("Set profile...")
        self.del_btn = QPushButton("Delete")
        self.bak_btn = QPushButton("Restore backup...")
        for b in (self.new_btn, self.ren_btn, self.pl_btn, self.del_btn, self.bak_btn):
            row2.addWidget(b)
        lay.addLayout(row2)

        # Cemu.exe / game path widgets: placed on the Settings tab by the main window
        self.exe_edit = QLineEdit(self.mgr.data.get("cemu_exe", "") or self.mgr.cemu_exe())
        self.game_edit = QLineEdit(self.mgr.data.get("game_path", ""))
        self.game_edit.setPlaceholderText(
            "Optional: the BotW game (.rpx / .wua) or its folder, so 'Launch BotW' starts straight in")
        eb = self.exe_browse = QPushButton("Browse...")
        gb = self.game_browse = QPushButton("File...")
        gd = self.game_dir_browse = QPushButton("Folder...")
        gd.setToolTip("Pick the game's folder (the one with code/, content/ and meta/ inside).")
        eb.clicked.connect(lambda: self._browse(self.exe_edit, "Cemu.exe", "Cemu (Cemu.exe);;All (*)"))
        gb.clicked.connect(lambda: self._browse(self.game_edit, "Game", "BotW (*.rpx *.wua *.wud *.wux);;All (*)"))
        gd.clicked.connect(self._browse_game_dir)
        for e in (self.exe_edit, self.game_edit):
            e.editingFinished.connect(self._store_launch)

        # launch bar: shown above the tabs by the main window
        box = QWidget()
        self.launch_box = box
        r = QHBoxLayout(box)
        r.setContentsMargins(0, 0, 0, 0)
        self.launch_btn = QPushButton("Launch Cemu")
        self.launch_btn.clicked.connect(lambda: self.launch_cemu(False))
        self.botw_btn = QPushButton("Launch BotW")
        self.botw_btn.setToolTip("Starts Cemu and loads the game set on the Settings tab straight away.")
        self.botw_btn.clicked.connect(lambda: self.launch_cemu(True))
        r.addWidget(self.launch_btn, 1)
        r.addWidget(self.botw_btn, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.switch_btn.clicked.connect(lambda: self.switch(launch=False))
        self.save_btn.clicked.connect(self.save_progress)
        self.new_btn.clicked.connect(self.new_instance)
        self.ren_btn.clicked.connect(self.rename_instance)
        self.pl_btn.clicked.connect(self.pick_playlist)
        self.del_btn.clicked.connect(self.delete_instance)
        self.bak_btn.clicked.connect(self.restore_backup)
        self.autodetect()
        self.refresh()

    # --- helpers
    def _browse(self, edit, title, flt):
        p, _ = QFileDialog.getOpenFileName(self, title, edit.text(), flt)
        if p:
            edit.setText(p)
            self._store_launch()

    def _browse_game_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Game folder", self.game_edit.text())
        if d:
            try:
                ki.InstanceManager.resolve_game(d)
            except Exception as e:
                QMessageBox.warning(self, "Game folder", str(e))
                return
            self.game_edit.setText(d)
            self._store_launch()

    def autodetect(self):
        """Fill in (and lock) Cemu.exe / the game .rpx when they can be found automatically."""
        exe = self.engine.cemu_dir / "Cemu.exe"
        if exe.exists():
            self.exe_edit.setText(str(exe))
            self._lock(self.exe_edit, self.exe_browse, True, "Found in the Cemu folder.")
        else:
            self._lock(self.exe_edit, self.exe_browse, False, "")
        rpx = None
        title = self.engine.user_dir / "mlc01" / "usr" / "title" / "00050000"
        for tid in ki.TITLE_LOWS:
            hits = sorted((title / tid / "code").glob("*.rpx")) if (title / tid / "code").is_dir() else []
            if hits:
                rpx = hits[0]
                break
        if rpx:
            self.game_edit.setText(str(rpx))
            self._lock(self.game_edit, self.game_browse, True, "Found in Cemu's mlc01 folder.")
            self.game_dir_browse.setEnabled(False)
        else:
            self._lock(self.game_edit, self.game_browse, False, "")
            self.game_dir_browse.setEnabled(True)
        self._store_launch()

    @staticmethod
    def _lock(edit, button, locked, tip):
        edit.setEnabled(not locked)
        button.setEnabled(not locked)
        edit.setToolTip(tip)

    def _store_launch(self):
        self.mgr.set_launch(self.exe_edit.text().strip().strip('"'), self.game_edit.text().strip().strip('"'))

    def selected(self):
        r = self.table.currentRow()
        it = self.table.item(r, 1) if r >= 0 else None
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def say(self, text):
        self.status.setText(text)

    def error(self, e):
        QMessageBox.warning(self, "Save Manager", str(e))

    def refresh(self, select=None):
        keep = select or self.selected()
        self.table.setRowCount(0)
        act = self.mgr.active()
        for iid in self.mgr.names():
            d = self.mgr.info(iid)
            r = self.table.rowCount()
            self.table.insertRow(r)
            vals = ["Active" if iid == act else "", d["name"],
                    d.get("last_played", "") or "never"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 1:
                    it.setData(Qt.ItemDataRole.UserRole, iid)
                self.table.setItem(r, c, it)
            if iid == keep:
                self.table.selectRow(r)
        if act:
            self.say(f'Active instance: "{self.mgr.info(act)["name"]}"   |   Live save: {self.mgr.live_save_dir()}')
        else:
            self.say(f"No active save slot yet.   Live save: {self.mgr.live_save_dir()}")
        self.refreshed.emit()

    # --- actions
    def need(self):
        iid = self.selected()
        if not iid:
            QMessageBox.information(self, "Save Manager", "Select a save slot first.")
        return iid

    def switch(self, launch):
        iid = self.need()
        if not iid:
            return
        try:
            msg = self.mgr.switch(iid)
        except ki.InstanceError as e:
            self.error(e)
            return
        except Exception as e:
            self.error(f"Something went wrong: {e}")
            return
        self.changed.emit()
        self.refresh(iid)
        self.say(msg)
        try:
            pending = self.engine.pending_changes()
        except Exception:
            pending = True
        has_pl = bool(self.mgr.info(iid).get("playlist"))
        if has_pl and pending:
            # apply the playlist automatically, then launch
            self._launch_after = launch
            self._msg = msg
            self.busy.emit(True, "Applying profile...")
            self.rebuilder = RebuildWorker(self.engine)
            self.rebuilder.progress.connect(lambda t: self.busy.emit(True, "Applying profile: " + t))
            self.rebuilder.done.connect(self.on_rebuilt)
            self.rebuilder.start()
        elif launch:
            self.do_launch(msg)

    def on_rebuilt(self, err):
        self.rebuilder.wait()
        self.busy.emit(False, "")
        self.changed.emit()
        if err:
            self.error(f"Profile couldn't be applied, so Cemu was not launched:\n{err}")
            return
        self.say(self._msg + "  Pack updated.")
        if self._launch_after:
            self.do_launch(self._msg + "  Pack updated.")

    def launch_cemu(self, with_game=False):
        self._store_launch()
        if with_game and not self.mgr.data.get("game_path"):
            QMessageBox.information(self, "Launch BotW", "Set the Game path first.")
            return
        if self.mgr.cemu_running():
            QMessageBox.information(self, "Launch Cemu", "Cemu is already running.")
            return
        self.do_launch("", with_game)

    def do_launch(self, msg, with_game=True):
        try:
            proc = self.mgr.launch(with_game)
        except ki.InstanceError as e:
            self.error(e)
            return
        self.say(msg + "  Cemu launched; progress is saved back when it closes.")
        self.watcher = CemuWatcher(proc)
        self.watcher.closed.connect(self.on_cemu_closed)
        self.watcher.start()

    def on_cemu_closed(self):
        try:
            if self.mgr.sync_back():
                self.say("Cemu closed. Progress saved into the active save slot.")
        except Exception as e:
            self.say(f"Couldn't save progress back: {e}")

    def save_progress(self):
        if not self.mgr.active():
            QMessageBox.information(self, "Save Manager", "No active save slot. Switch to one first.")
            return
        if self.mgr.sync_back():
            self.say("Progress saved into the active save slot.")
        else:
            self.say("Nothing to save (live save folder is empty).")

    def _playlist_choice(self, current=None):
        names = [NO_PLAYLIST] + self.engine.profile_names()
        cur = names.index(current) if current in names else 0
        name, ok = QInputDialog.getItem(
            self, "Profile", "Mod profile for this save slot (applied automatically when you switch):",
            names, cur, False)
        if not ok:
            return None
        return "" if name == NO_PLAYLIST else name

    def new_instance(self):
        name, ok = QInputDialog.getText(self, "New save slot", "Name:")
        if not ok or not name.strip():
            return
        src, ok = QInputDialog.getItem(
            self, "New save slot", "Starting save:",
            ["Copy of my current Cemu save", "Brand-new game (empty save)"], 0, False)
        if not ok:
            return
        pl = self._playlist_choice(None)
        if pl is None:
            return
        try:
            iid = self.mgr.create(name, "current" if src.startswith("Copy") else "empty", pl)
            self.refresh(iid)
        except ki.InstanceError as e:
            self.error(e)

    def rename_instance(self):
        iid = self.need()
        if not iid:
            return
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=self.mgr.info(iid)["name"])
        if ok:
            try:
                self.mgr.rename(iid, name)
                self.refresh(iid)
            except ki.InstanceError as e:
                self.error(e)

    def pick_playlist(self):
        iid = self.need()
        if not iid:
            return
        pl = self._playlist_choice(self.mgr.info(iid).get("playlist"))
        if pl is not None:
            self.mgr.set_playlist(iid, pl)
            self.refresh(iid)

    def delete_instance(self):
        iid = self.need()
        if not iid:
            return
        n = self.mgr.info(iid)["name"]
        if QMessageBox.question(self, "Delete save slot",
                                f'Delete "{n}" and its stored save? This cannot be undone.') \
                == QMessageBox.StandardButton.Yes:
            self.mgr.delete(iid)
            self.refresh()

    def restore_backup(self):
        bks = self.mgr.list_backups()
        if not bks:
            QMessageBox.information(self, "Backups", "No backups yet. korok makes one before every switch.")
            return
        name, ok = QInputDialog.getItem(self, "Restore backup", "Put this backup back as the live Cemu save:",
                                        [b.name for b in bks], 0, False)
        if ok:
            try:
                self.mgr.restore_backup(next(b for b in bks if b.name == name))
                self.say("Backup restored into Cemu's save folder.")
            except ki.InstanceError as e:
                self.error(e)

    def shutdown(self):
        try:
            if self.mgr.active() and not self.mgr.cemu_running():
                self.mgr.sync_back()
        except Exception:
            pass


# ===================================================================== #
class SaveEditorTab(QWidget):
    def __init__(self, engine, mgr, parent=None):
        super().__init__(parent)
        self.engine, self.mgr = engine, mgr
        self.schema = None
        self.save = None
        self.fields = {}
        lay = QVBoxLayout(self)

        warn = QLabel(
            "Close Cemu before editing. korok backs up the save file before every write and "
            "refuses to write if anything looks wrong. This editor has had limited testing "
            "against real saves, so keep your own copy too.")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        top = QHBoxLayout()
        self.file_combo = QComboBox()
        self.file_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.load_btn = QPushButton("Load")
        self.browse_btn = QPushButton("Open file...")
        self.reload_btn = QPushButton("Rescan")
        top.addWidget(QLabel("Save:"))
        top.addWidget(self.file_combo, stretch=1)
        for b in (self.load_btn, self.browse_btn, self.reload_btn):
            top.addWidget(b)
        lay.addLayout(top)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, stretch=1)

        # essentials
        self.core_page = QWidget()
        self.core_form = QFormLayout(self.core_page)
        self.tabs.addTab(self.core_page, "Essentials")

        # inventory
        inv = QWidget(); il = QVBoxLayout(inv)
        self.inv_table = QTableWidget(0, 4)
        self.inv_table.setHorizontalHeaderLabels(["Slot", "Item ID", "Count / Durability", "Equipped"])
        self.inv_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        il.addWidget(self.inv_table)
        ir = QHBoxLayout()
        self.add_item_btn = QPushButton("Add item to free slot...")
        ir.addWidget(self.add_item_btn); ir.addStretch(1)
        il.addLayout(ir)
        il.addWidget(QLabel("Item IDs are the game's internal names, e.g. Item_Sword_001. "
                            "The number is the stack size (or durability for weapons)."))
        self.tabs.addTab(inv, "Inventory")

        # flags
        fl = QWidget(); fv = QVBoxLayout(fl)
        sr = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search flag names...")
        self.type_filter = QComboBox()
        self.type_filter.addItems(["all types", "bool", "s32", "f32", "string", "vector"])
        sr.addWidget(self.search, stretch=1); sr.addWidget(self.type_filter)
        fv.addLayout(sr)
        self.flag_table = QTableWidget(0, 4)
        self.flag_table.setHorizontalHeaderLabels(["Flag", "Type", "Index", "Value"])
        self.flag_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        fv.addWidget(self.flag_table)
        self.flag_note = QLabel("")
        fv.addWidget(self.flag_note)
        self.tabs.addTab(fl, "All flags")

        row = QHBoxLayout()
        self.write_btn = QPushButton("Write changes to save")
        self.revert_btn = QPushButton("Discard changes")
        row.addStretch(1); row.addWidget(self.revert_btn); row.addWidget(self.write_btn)
        lay.addLayout(row)

        self.load_btn.clicked.connect(self.load_selected)
        self.browse_btn.clicked.connect(self.browse)
        self.reload_btn.clicked.connect(self.rescan)
        self.write_btn.clicked.connect(self.write)
        self.revert_btn.clicked.connect(self.revert)
        self.search.textChanged.connect(self.fill_flags)
        self.type_filter.currentIndexChanged.connect(self.fill_flags)
        self.add_item_btn.clicked.connect(self.add_item)
        self.flag_table.itemChanged.connect(self.on_flag_edited)
        self.inv_table.itemChanged.connect(self.on_inv_edited)
        self._loading = False
        self.rescan()

    # --- schema / files
    def ensure_schema(self) -> bool:
        if self.schema is not None:
            return True
        if not ks.available():
            QMessageBox.warning(self, "Save editor", "The save editor needs oead:  pip install oead")
            return False
        try:
            cache = ki.CONFIG_DIR / "korok_schema_cache.json"
            self.schema = ks.load_schema(self.engine._vanilla_roots(), cache)
            return True
        except Exception as e:
            QMessageBox.warning(self, "Save editor", f"Couldn't read flag definitions from your game files:\n{e}")
            return False

    def rescan(self):
        self.file_combo.clear()
        root = self.mgr.live_save_dir()
        for p in self.mgr.game_data_files(root):
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            self.file_combo.addItem(str(rel), str(p))
        self.info.setText(f"Looking in: {root}" if self.file_combo.count()
                          else f"No game_data.sav found in {root}. Use Open file... or play the game once.")

    def browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open game_data.sav", str(self.mgr.live_save_dir()), "game_data.sav (game_data.sav);;All (*)")
        if p:
            self.file_combo.addItem(p, p)
            self.file_combo.setCurrentIndex(self.file_combo.count() - 1)
            self.load_selected()

    def load_selected(self):
        if self.save is not None and self.save.dirty():
            if QMessageBox.question(self, "Unsaved changes", "Discard your unwritten changes?") != QMessageBox.StandardButton.Yes:
                return
        path = self.file_combo.currentData()
        if not path or not self.ensure_schema():
            return
        try:
            self.save = ks.SaveFile.load(path, self.schema)
        except ks.SaveError as e:
            QMessageBox.warning(self, "Save editor", str(e)); return
        except Exception as e:
            QMessageBox.warning(self, "Save editor", f"Couldn't read the save:\n{e}"); return
        self.info.setText(
            f"Loaded {path}\n{'big' if self.save.big else 'little'}-endian, "
            f"{self.save.match_ratio:.0%} of records recognised, "
            f"{self.save.unknown_hash_count()} unknown (left untouched).")
        self.build_core(); self.fill_inventory(); self.fill_flags()

    # --- essentials
    def build_core(self):
        while self.core_form.rowCount():
            self.core_form.removeRow(0)
        self.fields = {}
        for label, cands, kind, lo, hi in ks.CORE_SCALARS:
            name = ks.core_scalar_name(self.save, cands)
            if not name:
                continue
            val = self.save.get(name)
            if kind == "int":
                w = QSpinBox(); w.setRange(-2**31, 2**31 - 1); w.setValue(int(val))
                w.valueChanged.connect(lambda v, n=name: self.set_flag(n, v))
            elif kind == "float":
                w = QDoubleSpinBox(); w.setRange(-1e9, 1e9); w.setDecimals(2); w.setValue(float(val))
                w.valueChanged.connect(lambda v, n=name: self.set_flag(n, v))
            elif kind == "str":
                w = QLineEdit(str(val))
                w.editingFinished.connect(lambda n=name, w=w: self.set_flag(n, w.text()))
            else:  # vec
                w = QLineEdit(", ".join(f"{x:.3f}" for x in val))
                w.editingFinished.connect(lambda n=name, w=w: self.set_vec(n, w))
            w.setToolTip(name)
            self.core_form.addRow(f"{label}  [{name}]", w)
            self.fields[name] = w
        if not self.fields:
            self.core_form.addRow(QLabel("None of the common fields were found. Use the All flags tab."))

    def set_flag(self, name, value, index=0):
        if self._loading or self.save is None:
            return
        try:
            self.save.set(name, value, index)
        except Exception as e:
            QMessageBox.warning(self, "Save editor", str(e))

    def set_vec(self, name, w):
        try:
            vals = [float(x) for x in w.text().replace(";", ",").split(",")]
            self.save.set(name, vals)
        except Exception as e:
            QMessageBox.warning(self, "Save editor", f"Enter numbers separated by commas.\n{e}")

    # --- inventory
    def fill_inventory(self):
        self._loading = True
        self.inv_table.setRowCount(0)
        for slot, name, val, eq in ks.inventory_rows(self.save):
            r = self.inv_table.rowCount(); self.inv_table.insertRow(r)
            it0 = QTableWidgetItem(str(slot)); it0.setFlags(it0.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it0.setData(Qt.ItemDataRole.UserRole, slot)
            self.inv_table.setItem(r, 0, it0)
            self.inv_table.setItem(r, 1, QTableWidgetItem(name))
            self.inv_table.setItem(r, 2, QTableWidgetItem(str(val)))
            c = QTableWidgetItem(); c.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            c.setCheckState(Qt.CheckState.Checked if eq else Qt.CheckState.Unchecked)
            self.inv_table.setItem(r, 3, c)
        self._loading = False

    def on_inv_edited(self, item):
        if self._loading or self.save is None:
            return
        r = item.row()
        slot = self.inv_table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        try:
            if item.column() == 1:
                self.save.set(ks.INVENTORY_NAMES, item.text().strip(), slot)
            elif item.column() == 2:
                self.save.set(ks.INVENTORY_VALUE, int(item.text()), slot)
            elif item.column() == 3:
                self.save.set(ks.INVENTORY_EQUIP, item.checkState() == Qt.CheckState.Checked, slot)
        except Exception as e:
            QMessageBox.warning(self, "Save editor", str(e))

    def add_item(self):
        if self.save is None:
            return
        free = ks.free_inventory_slots(self.save)
        if not free:
            QMessageBox.information(self, "Inventory", "No free inventory slots."); return
        name, ok = QInputDialog.getText(self, "Add item", "Item ID (e.g. Item_Fruit_A):")
        if not ok or not name.strip():
            return
        cnt, ok = QInputDialog.getInt(self, "Add item", "Count:", 1, 0, 999999)
        if not ok:
            return
        try:
            self.save.set(ks.INVENTORY_NAMES, name.strip(), free[0])
            if self.save.has(ks.INVENTORY_VALUE):
                self.save.set(ks.INVENTORY_VALUE, cnt, free[0])
            self.fill_inventory()
        except Exception as e:
            QMessageBox.warning(self, "Save editor", str(e))

    # --- flag browser
    def fill_flags(self, *_):
        if self.save is None:
            return
        q = self.search.text().strip().lower()
        tf = self.type_filter.currentText()
        self._loading = True
        self.flag_table.setRowCount(0)
        shown = 0
        LIMIT = 800
        for fd, cnt in self.save.known_flags():
            if q and q not in fd.name.lower():
                continue
            if tf != "all types" and not fd.kind.startswith(tf):
                continue
            for idx in range(min(cnt, 1 if not q else 64)):
                if shown >= LIMIT:
                    break
                r = self.flag_table.rowCount(); self.flag_table.insertRow(r)
                a = QTableWidgetItem(fd.name); a.setFlags(a.flags() & ~Qt.ItemFlag.ItemIsEditable)
                a.setData(Qt.ItemDataRole.UserRole, (fd.name, idx))
                b = QTableWidgetItem(fd.kind + ("[]" if cnt > 1 else "")); b.setFlags(b.flags() & ~Qt.ItemFlag.ItemIsEditable)
                c = QTableWidgetItem(str(idx) if cnt > 1 else ""); c.setFlags(c.flags() & ~Qt.ItemFlag.ItemIsEditable)
                v = self.save.get(fd.name, idx)
                d = QTableWidgetItem(self.fmt(v))
                for col, it in enumerate((a, b, c, d)):
                    self.flag_table.setItem(r, col, it)
                shown += 1
            if shown >= LIMIT:
                break
        self._loading = False
        self.flag_note.setText(
            f"Showing {shown} rows." + (" (Limit reached; refine your search.)" if shown >= LIMIT else "")
            + ("  Arrays show element 0 until you search." if not q else ""))

    @staticmethod
    def fmt(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, tuple):
            return ", ".join(f"{x:.4f}" for x in v)
        if isinstance(v, float):
            return repr(v)
        return str(v)

    def on_flag_edited(self, item):
        if self._loading or self.save is None or item.column() != 3:
            return
        name, idx = self.flag_table.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)
        fd = self.schema.by_name[name]
        t = item.text().strip()
        try:
            if fd.kind == "bool":
                if t.lower() not in ("true", "false", "1", "0"):
                    raise ValueError("Use true or false.")
                val = t.lower() in ("true", "1")
            elif fd.kind == "s32":
                val = int(t)
            elif fd.kind == "f32":
                val = float(t)
            elif fd.kind.startswith("vector"):
                val = [float(x) for x in t.replace(";", ",").split(",")]
            else:
                val = t
            self.save.set(name, val, idx)
        except Exception as e:
            QMessageBox.warning(self, "Save editor", str(e))
            self._loading = True
            item.setText(self.fmt(self.save.get(name, idx)))
            self._loading = False

    # --- write / revert
    def revert(self):
        if self.save is not None:
            self.save.revert()
            self.build_core(); self.fill_inventory(); self.fill_flags()

    def write(self):
        if self.save is None:
            return
        if not self.save.dirty():
            QMessageBox.information(self, "Save editor", "No changes to write."); return
        if self.mgr.cemu_running():
            QMessageBox.warning(self, "Save editor", "Cemu is running. Close it first."); return
        n = len(self.save.changed_offsets())
        if QMessageBox.question(self, "Write changes", f"Write {n} changed byte(s) to\n{self.save.path}\n(a backup is made first)?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            bk = self.save.save(backup_dir=ki.BACKUPS_DIR / "editor")
            QMessageBox.information(self, "Saved", f"Done. Backup: {bk}")
            if self.mgr.active():
                self.mgr.sync_back()
        except Exception as e:
            QMessageBox.critical(self, "Save editor", f"Not written:\n{e}")
