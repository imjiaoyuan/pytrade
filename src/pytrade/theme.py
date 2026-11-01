LEGACY_PALETTE = {
    "buy_volume": "#a4a4a4",
    "buy_volume_text": "#ffffff",
    "sell_volume": "#a4a4a4",
    "sell_volume_text": "#ffffff",
    "price_gap": "#c4c4ce",
    "price_text": "#ffffff",
    "last_price": "#c4c4ce",
    "last_price_text": "#ffffff",
    "ask_price": "#ff5156",
    "bid_price": "#4945ff",
    "sidebar": "#ced9ee",
    "topbar": "#d7e2f3",
    "topbar_text": "#59647c",
    "summary": "#ff5156",
    "summary_text": "#000000",
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
