# gui/main_app.py
from PyQt5 import QtWidgets, QtGui, QtCore
from gui.main_window import Ui_MainWindow

from utils.file_handler import get_image_file
from api.local_ai_api import classify_image
from db.db import (
    ensure_schema, get_db_path,
    insert_result, upsert_result,
    fetch_results, search_results, delete_results
)
from utils.config import DEFECT_LABELS

from pathlib import Path

import os
import sqlite3
import pandas as pd

# 통계 대시보드 연결
from gui.stats_view import StatsDashboard

# Action에 Pass 추가 (정상일 때)
ACTIONS = ["Pass", "Rework", "Scrap", "Hold", "Reject"]


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        try:
            ensure_schema()
            print("[DB PATH]", get_db_path())
        except Exception as e:
            print("[DB] ensure_schema error:", e)

        self.current_image_path = None
        self._batch_files = []
        self._batch_idx = -1
        self._last_classify = None
        self._last_search = None

        self._prepare_table_headers()

        t = self.ui.tableResults
        t.setSelectionBehavior(t.SelectRows)
        t.setEditTriggers(t.NoEditTriggers)
        t.horizontalHeader().setStretchLastSection(False)
        t.setSortingEnabled(True)
        t.cellDoubleClicked.connect(self._on_row_dbl_clicked)

        self.ui.btnUpload.clicked.connect(self.on_upload_image)
        self.ui.pushButton.clicked.connect(self.on_upload_folder)
        self.ui.btnClassify.clicked.connect(self.on_classify)
        self.ui.btnSave.clicked.connect(self.on_save)
        self.ui.btnView.clicked.connect(self.on_view_results)

        self._ensure_toolbar_for_search_and_delete()
        self._refresh_results()

    # -------- UI 초기화 --------
    def _prepare_table_headers(self):
        t = self.ui.tableResults
        t.setColumnCount(9)
        headers = [
            "ID", "File Name", "Defect Type", "Severity",
            "Location", "Score (0~1)", "Detail", "Action", "Timestamp"
        ]  # 🔄 Instances → Location
        for i, h in enumerate(headers):
            item = QtWidgets.QTableWidgetItem(h)
            t.setHorizontalHeaderItem(i, item)

        header = t.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)  # ID
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)  # Location
        header.setSectionResizeMode(6, QtWidgets.QHeaderView.Stretch)           # Detail

    # -------- 이벤트 --------
    def on_upload_image(self):
        path = get_image_file()
        if not path:
            return
        self._batch_files = []
        self._batch_idx = -1
        self.current_image_path = path
        self._set_preview(path)
        self.ui.txtResult.clear()
        self._last_classify = None

    def on_classify(self):
        if not self.current_image_path:
            QtWidgets.QMessageBox.information(self, "안내", "먼저 이미지를 업로드하세요.")
            return
        self.ui.txtResult.setPlainText("불량 유형 분류 중…")
        result = classify_image(self.current_image_path)  # label, confidence, description, severity, location, action
        self._last_classify = result
        self.ui.txtResult.setPlainText(result.get("description") or "")

    def on_save(self):
        if not self.current_image_path:
            QtWidgets.QMessageBox.information(self, "안내", "이미지를 먼저 업로드하세요.")
            return
        if not hasattr(self, "_last_classify") or not self._last_classify:
            QtWidgets.QMessageBox.information(self, "안내", "분류 결과가 없습니다.")
            return

        desc = self.ui.txtResult.toPlainText().strip()
        if not desc:
            QtWidgets.QMessageBox.information(self, "안내", "저장할 설명이 없습니다.")
            return

        result = self._last_classify

        # Defect Type
        defect_type = result.get("label") or DEFECT_LABELS[0]
        if defect_type not in DEFECT_LABELS:
            defect_type = DEFECT_LABELS[0]

        # Score
        try:
            score = float(result.get("confidence") or 0.0)
        except Exception:
            score = 0.0

        # Severity (DB는 A/B/C)
        severity = result.get("severity", "C")
        if severity not in ["A", "B", "C"]:
            severity = "C"

        # Location
        location = (result.get("location") or "unknown").strip()

        # Action
        action = result.get("action", "Hold")
        if action not in ACTIONS:
            action = "Hold"

        # 정상(불량 없음) 규칙: action=Pass, severity=C, location=none
        if defect_type in {"none", "ok", "normal", "no_defect"}:
            action = "Pass"
            severity = "C"
            location = "none"

        try:
            abs_path = str(Path(self.current_image_path).resolve())
            upsert_result(
                abs_path,
                defect_type,
                severity,
                location,  
                score,
                desc,
                action
            )
            QtWidgets.QMessageBox.information(self, "완료", "DB 저장 완료.")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "DB 오류", f"저장 중 오류 발생: {e}")
            return

        self._refresh_results()
        self._advance_batch_if_any()

    def on_view_results(self):
        rows = fetch_results(limit=200)
        self._last_search = None
        self._render_rows(rows)

    # -------- 폴더 업로드 --------
    def on_upload_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select image folder")
        if not folder:
            return

        base = Path(folder)
        candidates = [
            str(p) for p in base.rglob("*")
            if p.is_file() and self._is_image_file(p)
        ]
        if not candidates:
            QtWidgets.QMessageBox.information(self, "No images", "선택한 폴더에 이미지가 없습니다.")
            return

        # 이미 저장된 경로 스킵
        try:
            existing_rows = fetch_results(limit=100000)
            existing_paths = {row[1] for row in existing_rows}  # image_path
        except Exception:
            existing_paths = set()

        unique_paths, seen = [], set()
        for f in candidates:
            if f in seen:
                continue
            seen.add(f)
            if f in existing_paths:
                continue
            unique_paths.append(f)

        if not unique_paths:
            QtWidgets.QMessageBox.information(self, "안내", "새로 저장할 이미지가 없습니다.")
            return

        prog = QtWidgets.QProgressDialog("폴더 내 일괄 판정/저장 중…", "취소", 0, len(unique_paths), self)
        prog.setWindowModality(QtCore.Qt.WindowModal)
        prog.setMinimumDuration(300)

        saved, errors = 0, 0

        for i, fpath in enumerate(unique_paths, start=1):
            if prog.wasCanceled():
                break
            try:
                self.current_image_path = fpath
                self._set_preview(fpath)

                result = classify_image(fpath)

                label = result.get("label") or DEFECT_LABELS[0]
                if label not in DEFECT_LABELS:
                    label = DEFECT_LABELS[0]

                try:
                    conf = float(result.get("confidence") or 0.0)
                except Exception:
                    conf = 0.0

                desc = result.get("description") or ""

                severity = result.get("severity", "C")
                if severity not in ["A", "B", "C"]:
                    severity = "C"

                location = (result.get("location") or "unknown").strip()

                action = result.get("action", "Hold")
                if action not in ACTIONS:
                    action = "Hold"

                # 정상(불량 없음) 규칙
                if label in {"none", "ok", "normal", "no_defect"}:
                    action = "Pass"
                    severity = "C"
                    location = "none"

                abs_path = str(Path(fpath).resolve())
                insert_result(
                    image_path=abs_path,
                    defect_type=label,
                    severity=severity,
                    location=location, 
                    score=conf,
                    detail=desc,
                    action=action
                )
                saved += 1
                self.ui.txtResult.setPlainText(desc)

            except Exception as e:
                print("[BATCH ERROR]", fpath, e)
                errors += 1

            prog.setValue(i)
            QtWidgets.QApplication.processEvents()

        prog.close()
        QtWidgets.QMessageBox.information(
            self, "완료",
            f"총 {len(unique_paths)}개 중 {saved}개 저장"
            + (f", 오류 {errors}개" if errors else "")
            + (", 취소됨" if saved + errors < len(unique_paths) else "")
        )
        self._refresh_results()

    # -------- 렌더링 --------
    def _render_rows(self, rows):
        """
        rows: (id, image_path, file_name, defect_type, severity, location, score, detail, action, ts)
        """
        t = self.ui.tableResults
        t.setUpdatesEnabled(False)
        sorting = t.isSortingEnabled()
        t.setSortingEnabled(False)

        t.clearContents()
        t.setRowCount(0)

        for row in rows:
            rid, image_path, file_name, defect_type, severity, location, score, detail, action, ts = row
            r = t.rowCount()
            t.insertRow(r)

            # ID: 숫자 정렬 되도록 EditRole에 int로 저장
            item_id = QtWidgets.QTableWidgetItem()
            item_id.setData(QtCore.Qt.EditRole, int(rid))
            t.setItem(r, 0, item_id)

            # File Name: 표시 텍스트 + UserRole에 '절대경로' 저장
            try:
                abs_image_path = str(Path(image_path).resolve())
            except Exception:
                abs_image_path = image_path or ""

            name_item = QtWidgets.QTableWidgetItem(file_name or "")
            name_item.setData(QtCore.Qt.UserRole, abs_image_path)  # ← 더블클릭 시 이 경로 사용
            t.setItem(r, 1, name_item)

            t.setItem(r, 2, QtWidgets.QTableWidgetItem(defect_type or ""))
            t.setItem(r, 3, QtWidgets.QTableWidgetItem(severity or ""))
            t.setItem(r, 4, QtWidgets.QTableWidgetItem(location or ""))

            # Score: 숫자 정렬 (표시는 %)
            item_score = QtWidgets.QTableWidgetItem()
            val = 0.0 if score is None else float(score)            
            item_score.setData(QtCore.Qt.EditRole, val)
            item_score.setText(f"{val:.1f}")
            item_score.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            t.setItem(r, 5, item_score)

            t.setItem(r, 6, QtWidgets.QTableWidgetItem(detail or ""))
            t.setItem(r, 7, QtWidgets.QTableWidgetItem(action or ""))
            t.setItem(r, 8, QtWidgets.QTableWidgetItem(ts or ""))

        t.setSortingEnabled(sorting)
        t.setUpdatesEnabled(True)
        t.viewport().update()

    # -------- 미리보기 --------
    def _set_preview(self, path: str):
        pix = QtGui.QPixmap(path)
        if pix.isNull():
            QtWidgets.QMessageBox.warning(self, "오류", "이미지를 불러오지 못했습니다.")
            return
        self.ui.lblImage.setPixmap(pix)
        self.ui.lblImage.setToolTip(path)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.current_image_path and self.ui.lblImage.pixmap():
            self.ui.lblImage.setPixmap(QtGui.QPixmap(self.current_image_path))

    def _on_row_dbl_clicked(self, row, col):
        # File Name 셀
        name_item = self.ui.tableResults.item(row, 1)
        if not name_item:
            return

        # UserRole(절대경로) → 셀 텍스트 → 절대경로 변환 순으로 폴백
        fpath = name_item.data(QtCore.Qt.UserRole) or name_item.text() or ""
        try:
            fpath = str(Path(fpath).resolve())
        except Exception:
            pass

        # Detail 패널 동기화
        detail_item = self.ui.tableResults.item(row, 6)
        if detail_item:
            self.ui.txtResult.setPlainText(detail_item.text())

        if QtCore.QFileInfo(fpath).exists():
            self._batch_files = []
            self._batch_idx = -1
            self.current_image_path = fpath
            self._set_preview(fpath)
        else:
            # 경로를 함께 보여줘서 디버깅 쉽게
            QtWidgets.QMessageBox.warning(self, "경고", f"로컬에 이미지 파일이 없습니다:\n{fpath}")

    # -------- 툴바 (검색/삭제) --------
    def _ensure_toolbar_for_search_and_delete(self):
        bars = self.findChildren(QtWidgets.QToolBar)
        tb = bars[0] if bars else QtWidgets.QToolBar("Main", self)
        if not bars:
            self.addToolBar(tb)

        self.actSearch = QtWidgets.QAction("Search", self)
        self.actSearch.setShortcut("Ctrl+F")
        self.actSearch.triggered.connect(self.on_search_dialog)
        tb.addAction(self.actSearch)
        self.addAction(self.actSearch)

        self.actDelete = QtWidgets.QAction("Delete Selected", self)
        self.actDelete.setShortcut("Del")
        self.actDelete.triggered.connect(self.on_delete_selected)
        tb.addAction(self.actDelete)
        self.addAction(self.actDelete)

        # --- ADD: Export DB (CSV) 액션 ---
        tb.addSeparator()
        self.actExportDBCSV = QtWidgets.QAction("Export DB (CSV)", self)
        self.actExportDBCSV.setShortcut("Ctrl+E")  # 원하면 변경/제거 가능
        self.actExportDBCSV.setStatusTip("Export all tables in app.db to CSV files")
        self.actExportDBCSV.triggered.connect(self.on_export_db_csv)
        tb.addAction(self.actExportDBCSV)
        self.addAction(self.actExportDBCSV)

    def on_search_dialog(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Search")
        form = QtWidgets.QFormLayout(dlg)

        edtType = QtWidgets.QLineEdit(dlg)
        edtSeverity = QtWidgets.QLineEdit(dlg)
        edtAction = QtWidgets.QLineEdit(dlg)
        edtLocation = QtWidgets.QLineEdit(dlg)
        edtKeyword = QtWidgets.QLineEdit(dlg)
        edtFrom = QtWidgets.QDateEdit(dlg); edtFrom.setCalendarPopup(True); edtFrom.setDisplayFormat("yyyy-MM-dd")
        edtFrom.setDate(QtCore.QDate.currentDate().addMonths(-1))
        edtTo = QtWidgets.QDateEdit(dlg); edtTo.setCalendarPopup(True); edtTo.setDisplayFormat("yyyy-MM-dd")
        edtTo.setDate(QtCore.QDate.currentDate())

        form.addRow("Defect Type:", edtType)
        form.addRow("Severity:", edtSeverity)
        form.addRow("Action:", edtAction)
        form.addRow("Location:", edtLocation)
        form.addRow("Keyword:", edtKeyword)
        form.addRow("From:", edtFrom)
        form.addRow("To:", edtTo)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, parent=dlg)
        form.addRow(btns)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)

        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return

        defect_type = edtType.text().strip() or None
        severity = edtSeverity.text().strip() or None
        action = edtAction.text().strip() or None
        location = edtLocation.text().strip() or None
        keyword = edtKeyword.text().strip() or None
        date_from = edtFrom.date().toString("yyyy-MM-dd")
        date_to = edtTo.date().toString("yyyy-MM-dd")

        try:
            rows = search_results(
                defect_type=defect_type,
                severity=severity,
                action=action,
                location=location,
                keyword=keyword,
                date_from=date_from,
                date_to=date_to,
                limit=500
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "검색 오류", str(e))
            return

        self._last_search = {
            "defect_type": defect_type,
            "severity": severity,
            "action": action,
            "location": location,
            "keyword": keyword,
            "date_from": date_from,
            "date_to": date_to,
        }
        self._render_rows(rows)

    def on_delete_selected(self):
        t = self.ui.tableResults
        sel = t.selectionModel().selectedRows()
        if not sel:
            QtWidgets.QMessageBox.information(self, "안내", "삭제할 행을 선택하세요.")
            return

        ids = []
        for idx in sel:
            rid_item = t.item(idx.row(), 0)
            if rid_item:
                try:
                    ids.append(int(rid_item.text()))
                except ValueError:
                    pass

        if not ids:
            QtWidgets.QMessageBox.information(self, "안내", "유효한 ID가 없습니다.")
            return

        reply = QtWidgets.QMessageBox.question(
            self, "삭제 확인", f"{len(ids)}개 항목을 삭제할까요?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return

        try:
            deleted = delete_results(ids)
            QtWidgets.QMessageBox.information(self, "완료", f"삭제됨: {deleted}개")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "삭제 오류", str(e))
            return

        self._refresh_results()
        self.ui.tableResults.clearSelection()
        self.ui.tableResults.scrollToTop()
        QtWidgets.QApplication.processEvents()

    def on_export_db_csv(self):
        try:
            # 1) DB 경로
            db_path = get_db_path()
            if not os.path.exists(db_path):
                QtWidgets.QMessageBox.warning(
                    self, "Export DB (CSV)",
                    f"DB 파일을 찾을 수 없습니다.\n{os.path.abspath(db_path)}"
                )
                return

            # 2) 저장할 폴더 선택 (테이블별 개별 CSV 저장)
            out_dir = QtWidgets.QFileDialog.getExistingDirectory(
                self, "폴더 선택 (테이블별 CSV 저장)"
            )
            if not out_dir:
                return

            saved_paths = []
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                # 사용자 테이블 목록 (sqlite 내부 테이블 제외)
                cur.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                """)
                tables = [r[0] for r in cur.fetchall()]

                if not tables:
                    QtWidgets.QMessageBox.information(self, "Export DB (CSV)", "내보낼 테이블이 없습니다.")
                    return

                # 테이블별 CSV 저장
                for tname in tables:
                    try:
                        df = pd.read_sql_query(f"SELECT * FROM {tname}", conn)
                        out_path = os.path.join(out_dir, f"{tname}.csv")
                        df.to_csv(out_path, index=False, encoding="utf-8-sig")  # 엑셀 호환 BOM
                        saved_paths.append(out_path)
                    except Exception as te:
                        print(f"[CSV EXPORT ERROR] table={tname} err={te}")

            if saved_paths:
                QtWidgets.QMessageBox.information(
                    self, "Export DB (CSV)", "저장 완료:\n" + "\n".join(saved_paths)
                )
            else:
                QtWidgets.QMessageBox.warning(self, "Export DB (CSV)", "저장된 파일이 없습니다.")

        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Export DB (CSV) 실패", str(e)) 
    
    # -------- 기타 --------
    def _is_image_file(self, path: Path) -> bool:
        return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}

    def _advance_batch_if_any(self):
        if not self._batch_files:
            return
        self._batch_idx += 1
        if self._batch_idx >= len(self._batch_files):
            QtWidgets.QMessageBox.information(self, "배치 완료", "폴더 내 이미지 처리를 모두 마쳤습니다.")
            self._batch_files = []
            self._batch_idx = -1
            return
        self.current_image_path = self._batch_files[self._batch_idx]
        self._set_preview(self.current_image_path)
        self.ui.txtResult.clear()
        self._last_classify = None

    def _refresh_results(self):
        try:
            if self._last_search:
                ctx = self._last_search
                rows = search_results(
                    defect_type=ctx.get("defect_type"),
                    severity=ctx.get("severity"),
                    action=ctx.get("action"),
                    location=ctx.get("location"),
                    keyword=ctx.get("keyword"),
                    date_from=ctx.get("date_from"),
                    date_to=ctx.get("date_to"),
                    limit=500
                )
            else:
                rows = fetch_results(limit=200)
            self._render_rows(rows)
        except Exception as e:
            print("[REFRESH ERROR]", e)

    # -------- 통계 대시보드 --------
    def on_view_results(self):
        dlg = StatsDashboard(get_db_path(), self)
        dlg.exec_()