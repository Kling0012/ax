"""ホスト見張り（S-11.5、S-11.6、D-10.3、F-14.2、F-18.3）。ホストの systemd の timer が host/ax-hostwatch.sh 経由で動かす。

標準ライブラリと axcommon（ids、timeutil、events、profile。標準ライブラリだけの部分）だけを使う。PYTHONPATH にリポジトリの根と host/lib を足して
`python3 -P -m hostwatch …` で動く。特権操作は Runner 経由の `sudo -n …` だけで、試験は偽の Runner に差し替える（実際の sudo・docker は呼ばない）。
"""
