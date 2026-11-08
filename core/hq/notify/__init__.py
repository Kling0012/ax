"""通知配送（B10。S-22.3、S-22.4、S-22.5、S-19.7、D-20、R-H16〜R-H25）。

HQ のコンテナの中で動く部品群。送れるのは ax-core の足を持つ部品だけ（F-9.1）。ここは axcommon.client の薄い送信（ntfy）と、
配送先ごとの枠、秘密・個人情報の検査、未配送の保持と再配送、質問の重複抑止、朝の要約、通知からの操作を持つ。

  policy.py      配送先ごとの方針（枠、検査の扱い、再配送）。ntfy の枠は S-17 `ntfy_rate_per_min`
  guard.py       送信前の秘密・個人情報の検査（検査できないときは配送先の方針に従う。Discord は送らない）
  ntfy.py        ntfy への POST（受付の HTTP 200 と ID を確かめる。テスト通知もここ）
  store.py       配送の保存先（SQLite）。通知の状態、質問の通知キー、操作の冪等キー
  dispatcher.py  送る・枠で保持する・再配送する。事象 question.notified、notification.failed、digest.generated
  question.py    未回答の質問の通知（ToDo ID と質問の版がキー。question_renotify_h の間は再通知しない）
  digest.py      朝の要約（対象日ごとに 1 回）
  actions.py     通知からの操作（開く、完了、保留）。台帳の更新を確かめてから成功とする
  intake.py      入口の hq 口への指示（Unix ソケット）。HQ は入口を通して書く（S-12.1）
  loop.py        周期の処理（質問の走査、再配送、朝の要約）

標準ライブラリと axcommon だけ（starlette は ext/b10a.py の HTTP の口だけ）。
"""
VERSION = "0.1.0"
COMPONENT = "hq"

# 通知の種別（S-22.4 の「通知種別」）
KIND_QUESTION = "question"      # 利用者に答えを求める ToDo（waiting_user）
KIND_TODO = "todo"              # その他の ToDo の知らせ
KIND_DIGEST = "digest"          # 朝の要約
KIND_TEST = "test"              # テスト通知（S-24 条件 4）
KINDS = (KIND_QUESTION, KIND_TODO, KIND_DIGEST, KIND_TEST)

DEST_NTFY = "ntfy"
DEST_DISCORD = "discord"
