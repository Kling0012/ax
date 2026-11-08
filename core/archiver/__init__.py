"""退避コネクタ archiver（S-14.3、D-13、F-20）。

各ボリュームの archive/ と、profile の archive_sources に登録した退避元から、古い物を退避先へコピーし、
コピー先で内容を照合し、一致したときだけ元を移す。削除の API は持たない。元を消す動作は既定で無効（reap-enabled の印が要る）。
固定アドレスは AX_FW_ARCHIVER_IP（ax-core。F-18.1。compose の ipv4_address）。
"""
VERSION = "0.1.0"
COMPONENT = "archiver"
