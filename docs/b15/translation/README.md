# 翻訳の検査と同期

外部翻訳は repo ごとの貢献型がある場合だけ受け取る。許可パス、ファイル数、サイズ、JSON、symlink、検査済み head を確認する。

同期の前に head を再確認する。head が移動した場合と同期 PR が複数ある場合は保留する。同期計画は非公開側の PR 入力と changelog fragment を作るだけで、push や PR 作成は行わない。

同期先だけにある言語は残す。`branch_cleanup` が無効な repo ではブランチ参照を退避しない。
