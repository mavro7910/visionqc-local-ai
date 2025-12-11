# gui/stats_view.py
from __future__ import annotations
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTabWidget, QWidget,
    QComboBox, QCheckBox, QMessageBox, QFileDialog
)
from PyQt5.QtCore import Qt

import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas


class StatsDashboard(QDialog):
    # 🔧 실제 테이블/컬럼명 상수
    TABLE        = "results"
    COL_DEFECT   = "defect_type"
    COL_SEVERITY = "severity"        # 값: A/B/C
    COL_TS       = "ts"              # YYYY-MM-DD HH:MM:SS 문자열
    COL_LOCATION = "location"
    COL_ACTION   = "action"

    def __init__(self, db_path: str, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.setWindowTitle("통계 대시보드")
        self.resize(1100, 720)

        root = QVBoxLayout(self)

        # ── 상단 요약 카드 ─────────────────────────────────────────────
        cards = QHBoxLayout()
        self.card_total = QLabel("Total: -")
        self.card_kinds = QLabel("Defect Types: -")
        self.card_week  = QLabel("Last 7 days: -")
        self.card_top   = QLabel("Top Defect: -")
        for c in (self.card_total, self.card_kinds, self.card_week, self.card_top):
            c.setAlignment(Qt.AlignCenter)
            c.setStyleSheet(
                "QLabel{border:1px solid #ddd; border-radius:8px; padding:12px; font-size:16px;}"
            )
            cards.addWidget(c)
        root.addLayout(cards)

        # ── 탭 생성 ────────────────────────────────────────────────────
        self.tabs = QTabWidget()
        self.tab1 = QWidget()
        self.tab2 = QWidget()
        self.tab3 = QWidget()
        self.tab4 = QWidget()
        self.tabs.addTab(self.tab1, "Defect Distribution")
        self.tabs.addTab(self.tab2, "Daily Trend")
        self.tabs.addTab(self.tab3, "Additional Metrics")
        self.tabs.addTab(self.tab4, "Location & Action")
        root.addWidget(self.tabs)

        # ── 탭별 Figure/Canvas (먼저 생성) ─────────────────────────────
        # constrained_layout=False + tight_layout()로 경고 방지
        self.fig1 = Figure(constrained_layout=False)
        self.canvas1 = FigureCanvas(self.fig1)
        layout1 = QVBoxLayout(self.tab1)
        layout1.addWidget(self.canvas1)

        self.fig2 = Figure(constrained_layout=False)
        self.canvas2 = FigureCanvas(self.fig2)
        layout2 = QVBoxLayout(self.tab2)
        layout2.addWidget(self.canvas2)

        self.fig3 = Figure(constrained_layout=False)
        self.canvas3 = FigureCanvas(self.fig3)
        layout3 = QVBoxLayout(self.tab3)
        layout3.addWidget(self.canvas3)

        self.fig4 = Figure(constrained_layout=False)
        self.canvas4 = FigureCanvas(self.fig4)
        layout4 = QVBoxLayout(self.tab4)
        layout4.addWidget(self.canvas4)

        # ── 탭1/탭2/탭3 UI 요소 ───────────────────────────────────────
        self._init_tab1_ui(layout1)
        self._init_tab2_ui(layout2)
        self._init_tab4_ui() 

        # ── 하단 버튼 ─────────────────────────────────────────────────
        bottom = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_png     = QPushButton("Save PNG")
        self.btn_csv     = QPushButton("Export CSV")
        self.btn_close   = QPushButton("Close")
        bottom.addWidget(self.btn_refresh)
        bottom.addWidget(self.btn_png)
        bottom.addWidget(self.btn_csv)
        bottom.addStretch()
        bottom.addWidget(self.btn_close)
        root.addLayout(bottom)

        # ── 시그널 ────────────────────────────────────────────────────
        self.btn_close.clicked.connect(self.close)
        self.btn_refresh.clicked.connect(self._refresh_all)
        self.cmb_period.currentIndexChanged.connect(self._draw_tab1_stacked)
        self.cmb_period2.currentIndexChanged.connect(self._draw_tab2_trend)
        self.chk_unknown.toggled.connect(self._draw_tab1_stacked)

        # 캐시 변수 Tab-wise CSV caches
        self._df_tab1 = None
        self._df_tab2 = None
        self._df_tab3_defect = None
        self._df_tab3_severity = None
        self._df_tab4_location = None
        self._df_tab4_action = None

        # 버튼 연결
        self.btn_png.clicked.connect(self.on_save_png)
        self.btn_csv.clicked.connect(self.on_export_csv)

        # ── 초기 로딩 ─────────────────────────────────────────────────
        self._refresh_all()

    # ─────────────────────────────────────────────────────────────────
    # 탭 UI 초기화
    # ─────────────────────────────────────────────────────────────────
    def _init_tab1_ui(self, layout: QVBoxLayout):
        ctrl = QHBoxLayout()
        self.cmb_period = QComboBox()
        self.cmb_period.addItem("Last 7 days", 7)
        self.cmb_period.addItem("Last 30 days", 30)
        self.cmb_period.addItem("All", None)
        self.chk_unknown = QCheckBox("Include Unknown")
        ctrl.addWidget(QLabel("Period:"))
        ctrl.addWidget(self.cmb_period)
        ctrl.addStretch()
        ctrl.addWidget(self.chk_unknown)
        layout.insertLayout(0, ctrl)

        # 보조 카드 (Severe Top3 / ≥Moderate Ratio)
        info = QHBoxLayout()
        self.card_top3 = QLabel("Severe Top3: -")
        self.card_modplus = QLabel("≥Moderate Ratio: -")
        for c in (self.card_top3, self.card_modplus):
            c.setAlignment(Qt.AlignCenter)
            c.setStyleSheet("QLabel{border:1px dashed #ccc; border-radius:8px; padding:10px;}")
        info.addWidget(self.card_top3)
        info.addWidget(self.card_modplus)
        layout.addLayout(info)

    def _init_tab2_ui(self, layout: QVBoxLayout):
        ctrl = QHBoxLayout()
        self.cmb_period2 = QComboBox()
        self.cmb_period2.addItem("Last 7 days", 7)
        self.cmb_period2.addItem("Last 30 days", 30)
        self.cmb_period2.addItem("All", None)
        ctrl.addWidget(QLabel("Period:"))
        ctrl.addWidget(self.cmb_period2)
        ctrl.addStretch()
        layout.insertLayout(0, ctrl)

    def _init_tab4_ui(self):
    # 기존 레이아웃이 있으면 제거
        old_layout = self.tab4.layout()
        if old_layout:
            QWidget().setLayout(old_layout)

        self.fig4 = Figure(constrained_layout=True)
        self.canvas4 = FigureCanvas(self.fig4)

        layout = QVBoxLayout()
        layout.addWidget(self.canvas4)
        self.tab4.setLayout(layout)
    # ─────────────────────────────────────────────────────────────────
    # 공용 유틸
    # ─────────────────────────────────────────────────────────────────
    def _ensure_table_exists(self) -> bool:
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            names = {r[0] for r in cur.fetchall()}
            return self.TABLE in names
        finally:
            try: conn.close()
            except: pass

    def _period_where_clause_for(self, combo: QComboBox) -> str:
        """QComboBox userData(일수)를 읽어 WHERE 절 생성"""
        days = combo.currentData()
        if days is None:
            return ""  # All
        return f"AND date(substr({self.COL_TS},1,10)) >= date('now','-{int(days)} day')"

    # ─────────────────────────────────────────────────────────────────
    # 전체 새로고침
    # ─────────────────────────────────────────────────────────────────
    def _refresh_all(self):
        if not self._ensure_table_exists():
            QMessageBox.warning(self, "DB Error", f"Table '{self.TABLE}' not found.\nCheck your database.")
            return
        self._load_summary_cards()
        self._draw_tab1_stacked()
        self._draw_tab2_trend()
        self._draw_tab3_pies()  
        self._draw_tab4_location_action()

    # ─────────────────────────────────────────────────────────────────
    # 상단 요약 카드
    # ─────────────────────────────────────────────────────────────────
    def _load_summary_cards(self):
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            cur.execute(f"SELECT COUNT(*) FROM {self.TABLE}")
            total = cur.fetchone()[0] or 0

            cur.execute(
                f"SELECT COUNT(DISTINCT {self.COL_DEFECT}) FROM {self.TABLE} "
                f"WHERE IFNULL({self.COL_DEFECT},'') <> ''"
            )
            kinds = cur.fetchone()[0] or 0

            cur.execute(
                f"SELECT COUNT(*) FROM {self.TABLE} "
                f"WHERE date(substr({self.COL_TS},1,10)) >= date('now','-7 day')"
            )
            week = cur.fetchone()[0] or 0

            cur.execute(
                f"SELECT {self.COL_DEFECT} FROM {self.TABLE} "
                f"WHERE IFNULL({self.COL_DEFECT},'') <> '' "
                f"GROUP BY {self.COL_DEFECT} ORDER BY COUNT(*) DESC LIMIT 1"
            )
            top = cur.fetchone()
            top = top[0] if top and top[0] else "-"

            self.card_total.setText(f"Total: {total}")
            self.card_kinds.setText(f"Defect Types: {kinds}")
            self.card_week.setText(f"Last 7 days: {week}")
            self.card_top.setText(f"Top Defect: {top}")

        except Exception as e:
            self.card_total.setText("Total: -")
            self.card_kinds.setText("Defect Types: -")
            self.card_week.setText("Last 7 days: -")
            self.card_top.setText("Top Defect: -")
            print("[SUMMARY ERROR]", e)
        finally:
            try: conn.close()
            except: pass

    # ─────────────────────────────────────────────────────────────────
    # 탭1: 결함×Severity 스택 막대
    # ─────────────────────────────────────────────────────────────────
    def _draw_tab1_stacked(self):
        if not hasattr(self, "fig1"):
            return

        self.fig1.clear()
        ax = self.fig1.add_subplot(111)

        include_unknown = self.chk_unknown.isChecked()
        period_sql = self._period_where_clause_for(self.cmb_period)

        sql_top = f"""
            SELECT {self.COL_DEFECT}, COUNT(*) AS total
            FROM {self.TABLE}
            WHERE IFNULL({self.COL_DEFECT},'') <> ''
            {period_sql}
            GROUP BY {self.COL_DEFECT}
            ORDER BY total DESC
            LIMIT 10
        """

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(sql_top)
            top_rows = cur.fetchall()
            top_defects = [r[0] for r in top_rows]

            if not top_defects:
                ax.text(0.5, 0.5, "No data to display.", ha="center", va="center")
                self.fig1.tight_layout()
                self.canvas1.draw()
                return

            placeholders = ",".join(["?"] * len(top_defects))
            sql_stack = f"""
                SELECT {self.COL_DEFECT}, {self.COL_SEVERITY}, COUNT(*) AS cnt
                FROM {self.TABLE}
                WHERE {self.COL_DEFECT} IN ({placeholders})
                {period_sql}
                GROUP BY {self.COL_DEFECT}, {self.COL_SEVERITY}
            """
            cur.execute(sql_stack, top_defects)
            rows = cur.fetchall()
        except Exception as e:
            ax.text(0.5, 0.5, f"DB error: {e}", ha="center", va="center")
            self.fig1.tight_layout()
            self.canvas1.draw()
            return
        finally:
            try: conn.close()
            except: pass

        def map_sev(s: str) -> str:
            s = (s or "").strip().upper()
            if s == "A": return "severe"
            if s == "B": return "moderate"
            if s == "C": return "minor"
            if s in ("SEVERE", "CRITICAL", "HIGH"): return "severe"
            if s in ("MODERATE", "MID", "MEDIUM"):  return "moderate"
            if s in ("MINOR", "LOW"):               return "minor"
            return "unknown"

        defects = top_defects[:]  # X축 순서 유지
        sev_levels = ["minor", "moderate", "severe"] + (["unknown"] if include_unknown else [])
        pivot = {d: {k: 0 for k in sev_levels} for d in defects}

        for d, s, c in rows:
            ms = map_sev(s)
            if ms not in sev_levels:
                if ms == "unknown" and include_unknown:
                    pivot[d]["unknown"] += int(c)
                continue
            pivot[d][ms] += int(c)

        x = np.arange(len(defects))
        bottoms = np.zeros(len(defects), dtype=int)

        # 기존 legend 중복 제거
        old_legend = ax.get_legend()
        if old_legend:
            old_legend.remove()

        # Severity별 색상 지정
        color_map = {
            "severe": "#e74c3c",    # 🔴 빨강 (A)
            "moderate": "#f39c12",  # 🟠 주황 (B)
            "minor": "#2ecc71",     # 🟢 초록 (C)
            "unknown": "#95a5a6"    # ⚪ 회색 (기타)
        }

        for sev in sev_levels:
            vals = np.array([pivot[d][sev] for d in defects])
            color = color_map.get(sev, "#7f8c8d")
            ax.bar(x, vals, bottom=bottoms, label=sev.capitalize(), color=color)
            bottoms += vals

        # 총건수 라벨
        totals = bottoms
        for i, v in enumerate(totals):
            if v > 0:
                ax.text(i, v, str(int(v)), ha="center", va="bottom", fontsize=9)

        ax.set_title("Defect Distribution (by Severity)")
        ax.set_xticks(x)
        ax.set_xticklabels(defects, rotation=20, ha="right")
        ax.set_ylabel("Count")
        ax.grid(True, axis="y", linestyle="--", alpha=0.4)

        # 범례(unknown은 숨김)
        handles, labels = ax.get_legend_handles_labels()
        by_label = {lab: h for h, lab in zip(handles, labels) if not lab.startswith("_")}
        if by_label:
            ax.legend(by_label.values(), by_label.keys(), title="Severity", loc="upper right")

        self.fig1.tight_layout()
        self.canvas1.draw()

        # --- CSV for Tab1: defect x severity pivot (one row per defect)
        cols = ["defect", "minor", "moderate", "severe"]
        if "unknown" in sev_levels:
            cols.append("unknown")
        rows_for_csv = []
        for d in defects:
            row = {
                "defect": d,
                "minor": pivot[d].get("minor", 0),
                "moderate": pivot[d].get("moderate", 0),
                "severe": pivot[d].get("severe", 0),
            }
            if "unknown" in sev_levels:
                row["unknown"] = pivot[d].get("unknown", 0)
            row["total"] = sum(pivot[d].values())
            rows_for_csv.append(row)
        self._df_tab1 = pd.DataFrame(rows_for_csv, columns=cols + ["total"])

        # 보조 카드
        rate_list = []
        for d in defects:
            s_cnt = pivot[d].get("severe", 0)
            tot = sum(pivot[d].values())
            rate = (s_cnt / tot * 100) if tot else 0.0
            rate_list.append((d, rate))
        rate_list.sort(key=lambda x: x[1], reverse=True)
        top3_txt = ", ".join([f"{n}({r:.0f}%)" for n, r in rate_list[:3]]) if rate_list else "-"

        sum_mod_sev = sum(pivot[d].get("moderate", 0) + pivot[d].get("severe", 0) for d in defects)
        sum_all = sum(sum(pivot[d].values()) for d in defects) or 1
        modplus_rate = sum_mod_sev / sum_all * 100

        self.card_top3.setText(f"Severe Top3: {top3_txt or '-'}")
        self.card_modplus.setText(f"≥Moderate Ratio: {modplus_rate:.1f}%")

    # ─────────────────────────────────────────────────────────────────
    # 탭2: 일자별 추이 (라인)
    # ─────────────────────────────────────────────────────────────────
    def _draw_tab2_trend(self):
        if not hasattr(self, "fig2"):
            return

        self.fig2.clear()
        ax = self.fig2.add_subplot(111)

        where_sql = self._period_where_clause_for(self.cmb_period2)
        sql = f"""
            SELECT substr({self.COL_TS},1,10) AS day, COUNT(*) AS cnt
            FROM {self.TABLE}
            WHERE 1=1
            {where_sql}
            GROUP BY day
            ORDER BY day
        """

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
        except Exception as e:
            ax.text(0.5, 0.5, f"DB error: {e}", ha="center", va="center")
            self.fig2.tight_layout()
            self.canvas2.draw()
            return
        finally:
            try: conn.close()
            except: pass

        if not rows:
            ax.text(0.5, 0.5, "No data to display.", ha="center", va="center")
            self.fig2.tight_layout()
            self.canvas2.draw()
            return

        # 날짜 파싱
        x_dates = [datetime.strptime(r[0], "%Y-%m-%d") for r in rows]
        y_counts = [int(r[1]) for r in rows]

        ax.plot(x_dates, y_counts, marker="o", linewidth=2)
        ax.set_title("Daily Volume")
        ax.set_xlabel("Date")
        ax.set_ylabel("Count")
        ax.grid(True, axis="y", linestyle="--", alpha=0.4)

        # 라벨 개수 제한 (가독성)
        if len(x_dates) > 20:
            step = max(1, len(x_dates) // 20)
            ax.set_xticks(x_dates[::step])

        # 날짜 포맷/자동 회전
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        self.fig2.autofmt_xdate(rotation=45)
        ax.margins(x=0.02)

        self.fig2.tight_layout()
        self.canvas2.draw()

        # --- ADD: CSV 내보내기용
        self._df_tab2 = pd.DataFrame({
            "day": [d.strftime("%Y-%m-%d") for d in x_dates],
            "count": y_counts
        })
    # ─────────────────────────────────────────────────────────────────
    # 탭3: 결함 비율 파이차트 + Severity 비율 파이차트
    # ─────────────────────────────────────────────────────────────────
    def _draw_tab3_pies(self):
        if not hasattr(self, "fig3"):
            return

        self.fig3.clear()
        ax1 = self.fig3.add_subplot(121)
        ax2 = self.fig3.add_subplot(122)

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # 결함 유형 비율
            cur.execute(f"""
                SELECT {self.COL_DEFECT}, COUNT(*) 
                FROM {self.TABLE}
                WHERE IFNULL({self.COL_DEFECT}, '') <> ''
                GROUP BY {self.COL_DEFECT}
                ORDER BY COUNT(*) DESC
                LIMIT 10
            """)
            rows_def = cur.fetchall()

            # Severity 비율
            cur.execute(f"""
                SELECT {self.COL_SEVERITY}, COUNT(*)
                FROM {self.TABLE}
                WHERE IFNULL({self.COL_SEVERITY}, '') <> ''
                GROUP BY {self.COL_SEVERITY}
            """)
            rows_sev = cur.fetchall()
            conn.close()

        except Exception as e:
            ax1.text(0.5, 0.5, f"DB Error: {e}", ha="center", va="center")
            ax2.text(0.5, 0.5, " ", ha="center", va="center")
            self.canvas3.draw()
            return

        # ----------- 결함 유형 파이 -----------
        if not rows_def:
            ax1.text(0.5, 0.5, "No data", ha="center", va="center")
        else:
            labels = [r[0] for r in rows_def]
            values = [r[1] for r in rows_def]
            ax1.pie(values, labels=labels, autopct="%1.0f%%", startangle=90)
            ax1.set_title("Defect Type Ratio (Top 10)")

        # ----------- Severity 파이 -----------
        if not rows_sev:
            ax2.text(0.5, 0.5, "No data", ha="center", va="center")
        else:
            labels2 = [r[0] for r in rows_sev]
            values2 = [r[1] for r in rows_sev]
            ax2.pie(values2, labels=labels2, autopct="%1.0f%%", startangle=90,
                    colors=["#a3e1d4", "#ffd777", "#ff9999"])
            ax2.set_title("Severity Ratio (A/B/C)")

        #self.fig3.tight_layout()
        self.canvas3.draw()

        # --- ADD: CSV 내보내기용
        self._df_tab3_defect = (
        pd.DataFrame(rows_def, columns=["defect_type", "count"])
        if rows_def else pd.DataFrame(columns=["defect_type","count"])
        )
        self._df_tab3_severity = (
            pd.DataFrame(rows_sev, columns=["severity", "count"])
            if rows_sev else pd.DataFrame(columns=["severity","count"])
        )
    # ─────────────────────────────────────────────────────────────────
    # 탭4: 히트맵 / 가로 막대 / 결함 위치별 비율
    # ─────────────────────────────────────────────────────────────────
    def _draw_tab4_location_action(self):
        if not hasattr(self, "fig4"):
            return

        self.fig4.clear()
        ax1 = self.fig4.add_subplot(121)
        ax2 = self.fig4.add_subplot(122)

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()

            # ① Location별 결함 건수
            cur.execute(f"""
                SELECT {self.COL_LOCATION}, COUNT(*) 
                FROM {self.TABLE}
                WHERE IFNULL({self.COL_LOCATION}, '') <> ''
                GROUP BY {self.COL_LOCATION}
                ORDER BY COUNT(*) DESC
                LIMIT 10
            """)
            rows_loc = cur.fetchall()

            # ② Action별 건수
            cur.execute(f"""
                SELECT {self.COL_ACTION}, COUNT(*)
                FROM {self.TABLE}
                WHERE IFNULL({self.COL_ACTION}, '') <> ''
                GROUP BY {self.COL_ACTION}
                ORDER BY COUNT(*) DESC
            """)
            rows_act = cur.fetchall()

            conn.close()

        except Exception as e:
            ax1.text(0.5, 0.5, f"DB Error: {e}", ha="center", va="center")
            ax2.text(0.5, 0.5, " ", ha="center", va="center")
            self.canvas4.draw()
            return

        # ----------- Location별 막대 -----------
        if not rows_loc:
            ax1.text(0.5, 0.5, "No data", ha="center", va="center")
        else:
            locs = [r[0] for r in rows_loc]
            vals = [r[1] for r in rows_loc]
            ax1.barh(locs, vals)
            ax1.invert_yaxis()  # 상위가 위로 오게
            ax1.set_title("Defect Count by Location")
            ax1.set_xlabel("Count")
            for i, v in enumerate(vals):
                ax1.text(v + 0.2, i, str(v), va="center")

        # ----------- Action 파이차트 -----------
        if not rows_act:
            ax2.text(0.5, 0.5, "No data", ha="center", va="center")
        else:
            acts = [r[0] for r in rows_act]
            counts = [r[1] for r in rows_act]
            ax2.pie(counts, labels=acts, autopct="%1.0f%%", startangle=90)
            ax2.set_title("Action Ratio")

        #self.fig4.tight_layout()
        self.canvas4.draw()

        # --- ADD: CSV 내보내기용
        self._df_tab4_location = (
            pd.DataFrame(rows_loc, columns=["location", "count"])
            if rows_loc else pd.DataFrame(columns=["location","count"])
        )
        self._df_tab4_action = (
            pd.DataFrame(rows_act, columns=["action", "count"])
            if rows_act else pd.DataFrame(columns=["action","count"])
        )

    # ─────────────────────────────────────────────────────────────────
    # Save PNG 함수
    # ─────────────────────────────────────────────────────────────────
    def _current_fig(self):
        idx = self.tabs.currentIndex()
        figs = [self.fig1, self.fig2, self.fig3, self.fig4]
        return figs[idx] if 0 <= idx < len(figs) else None

    def on_save_png(self):
        try:
            # 현재 탭 기준 Figure
            fig = self._current_fig()
            if fig is None:
                QMessageBox.warning(self, "Save PNG", "저장할 차트를 찾지 못했습니다.")
                return

            # 파일 저장 다이얼로그
            path, _ = QFileDialog.getSaveFileName(
                self, "Save chart as PNG", "dashboard.png", "PNG Image (*.png)"
            )
            if not path:
                return

            fig.savefig(path, dpi=200, bbox_inches="tight")
            QMessageBox.information(self, "Save PNG", f"이미지를 저장했습니다.\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Save PNG 실패", str(e))

    
    # ─────────────────────────────────────────────────────────────────
    # Export CSV 함수
    # ─────────────────────────────────────────────────────────────────
    def on_export_csv(self):
        try:
            idx = self.tabs.currentIndex()

            # 탭1: 한 파일
            if idx == 0:
                df = self._df_tab1
                if df is None or df.empty:
                    QMessageBox.warning(self, "Export CSV", "내보낼 데이터가 없습니다.")
                    return
                path, _ = QFileDialog.getSaveFileName(
                    self, "Export CSV", "defect_distribution.csv", "CSV Files (*.csv)"
                )
                if not path: return
                df.to_csv(path, index=False, encoding="utf-8-sig")
                QMessageBox.information(self, "Export CSV", f"저장 완료:\n{path}")
                return

            # 탭2: 한 파일
            if idx == 1:
                df = self._df_tab2
                if df is None or df.empty:
                    QMessageBox.warning(self, "Export CSV", "내보낼 데이터가 없습니다.")
                    return
                path, _ = QFileDialog.getSaveFileName(
                    self, "Export CSV", "daily_trend.csv", "CSV Files (*.csv)"
                )
                if not path: return
                df.to_csv(path, index=False, encoding="utf-8-sig")
                QMessageBox.information(self, "Export CSV", f"저장 완료:\n{path}")
                return

            # 탭3: 두 파일 (폴더 선택)
            if idx == 2:
                if (self._df_tab3_defect is None or self._df_tab3_severity is None or
                    (self._df_tab3_defect.empty and self._df_tab3_severity.empty)):
                    QMessageBox.warning(self, "Export CSV", "내보낼 데이터가 없습니다.")
                    return
                folder = QFileDialog.getExistingDirectory(self, "폴더 선택 (탭3 CSV 2개 저장)")
                if not folder: return

                saved = []
                if self._df_tab3_defect is not None and not self._df_tab3_defect.empty:
                    p1 = os.path.join(folder, "defect_type_ratio.csv")
                    self._df_tab3_defect.to_csv(p1, index=False, encoding="utf-8-sig")
                    saved.append(p1)
                if self._df_tab3_severity is not None and not self._df_tab3_severity.empty:
                    p2 = os.path.join(folder, "severity_ratio.csv")
                    self._df_tab3_severity.to_csv(p2, index=False, encoding="utf-8-sig")
                    saved.append(p2)

                if saved:
                    QMessageBox.information(self, "Export CSV", "저장 완료:\n" + "\n".join(saved))
                else:
                    QMessageBox.warning(self, "Export CSV", "저장할 유효한 데이터가 없습니다.")
                return

            # 탭4: 두 파일 (폴더 선택)
            if idx == 3:
                if (self._df_tab4_location is None and self._df_tab4_action is None) or \
                ((self._df_tab4_location is not None and self._df_tab4_location.empty) and
                    (self._df_tab4_action is not None and self._df_tab4_action.empty)):
                    QMessageBox.warning(self, "Export CSV", "내보낼 데이터가 없습니다.")
                    return

                folder = QFileDialog.getExistingDirectory(self, "폴더 선택 (탭4 CSV 2개 저장)")
                if not folder: return

                saved = []
                if self._df_tab4_location is not None and not self._df_tab4_location.empty:
                    p1 = os.path.join(folder, "location_count.csv")
                    self._df_tab4_location.to_csv(p1, index=False, encoding="utf-8-sig")
                    saved.append(p1)
                if self._df_tab4_action is not None and not self._df_tab4_action.empty:
                    p2 = os.path.join(folder, "action_ratio.csv")
                    self._df_tab4_action.to_csv(p2, index=False, encoding="utf-8-sig")
                    saved.append(p2)

                if saved:
                    QMessageBox.information(self, "Export CSV", "저장 완료:\n" + "\n".join(saved))
                else:
                    QMessageBox.warning(self, "Export CSV", "저장할 유효한 데이터가 없습니다.")
                return

        except Exception as e:
            QMessageBox.critical(self, "Export CSV 실패", str(e))