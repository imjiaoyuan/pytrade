LEGACY_PALETTE = {
    "buy_volume": "#e2555a",
    "buy_volume_text": "#ffffff",
    "sell_volume": "#37a06b",
    "sell_volume_text": "#ffffff",
    "price_gap": "#f0f1f4",
    "price_text": "#20242c",
    "last_price": "#ffd83d",
    "last_price_text": "#27211a",
    "sidebar": "#e9ebf0",
    "topbar": "#232a36",
    "topbar_text": "#f2f4f8",
    "summary": "#d8f7b0",
    "summary_text": "#173806",
    "side_button_buy": "#e2555a",
    "side_button_sell": "#7fb1ea",
    "quote_panel": "#d7dbe3",
    "mark_open": "#1f6fd1",
    "mark_open_text": "#ffffff",
    "mark_fill": "#c43d8f",
    "mark_fill_text": "#ffffff",
}

COMBO_POPUP_STYLE = """
QComboBox { combobox-popup: 0; }
QComboBox QAbstractItemView {
    background-color: #ffffff;
    color: #202020;
    selection-background-color: #167ca6;
    selection-color: #ffffff;
    outline: 0;
}
QComboBox QAbstractItemView::item:selected {
    background-color: #167ca6;
    color: #ffffff;
}
"""

APPLICATION_STYLE = "QWidget { background-color:#eef0f0; } QLineEdit, QSpinBox, QDoubleSpinBox, QTableWidget { background-color:white; } QHeaderView::section { padding:2px; border:1px solid #cdd0d0; } QTableView { gridline-color:#b6c3b4; }" + COMBO_POPUP_STYLE
