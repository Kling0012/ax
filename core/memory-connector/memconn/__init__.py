"""記憶コネクタ memory-connector（S-14.2、D-13、F-8）。すべての部品は agent-memory に直接書かず、ここを通す。

ディレクトリ名にハイフンがあるため、起動は `python /ax/core/memory-connector/main.py`（main.py が親ディレクトリを sys.path に足す）。
"""
VERSION = "0.1.0"
COMPONENT = "memory-connector"
