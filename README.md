# a16z Digest

a16zの発信とポートフォリオ、科学・工学メディアを毎日収集し、毎週月曜の朝にタブ切り替え式のサイトを更新して、通知メールを送る仕組み。

**流れ**: RSS収集（毎日）→ Claudeで記事を構造化 → キーワードの初出と急上昇を集計 → 編集者プロンプトで考察 → `site/index.html`を生成 → GitHub Pagesにデプロイ → Gmailで要点とリンクを通知

**タブ**: 総合（新規投資先を含む）/ つながり（グラフ）/ 企業（動きのある企業と言及ランキング）/ 関心トラック（config.yamlの`interests`）/ キーワード / 予測ログ。過去の号は週セレクタで切り替えられる。

**ソース（第1段階）**: a16z.com、a16z News、American Dynamism、a16zポートフォリオ、Quanta Magazine、IEEE Spectrum、The Robot Report、EE Times

本実装の手順は `CLAUDE.md` を参照。

## セットアップ
1. このフォルダをGitHubリポジトリにpushする
   - GitHub Pagesをprivateリポジトリで使うには有料プラン（Pro以上）が必要
   - publicにする場合は、`data/`（記事要約DB）と`config.yaml`（読者像）も公開される
2. Settings → Pages → Source を「GitHub Actions」にする
3. Gmailで2段階認証を有効化 → アプリパスワードを発行
4. Settings → Secrets に `ANTHROPIC_API_KEY` / `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` / `MAIL_TO`（省略時は自分宛）を登録
5. `config.yaml`の`site_url`をPagesのURLに書き換える
6. ローカルで初回実行（ベースライン作成とプレビュー）
   ```
   pip install -r requirements.txt
   python digest.py --backfill-days 35 --dry-run   # site/index.html をブラウザで確認
   git add data && git commit -m "baseline" && git push
   ```
7. ActionsタブからRun workflowで手動実行し、サイト更新とメール到着を確認

## 関心の編集
`config.yaml`の`interests`にトラック（name / purpose / keywords）を追加・編集する。タブは自動で増減する。

## 次の拡張候補
- 自分の保有銘柄・気になる企業のウォッチ（保有情報は公開リポジトリに置かない）
- 関心キーワードを更新する仕組み（メール返信や月次見直し）
- Podcast/YouTube文字起こしの取り込み
- YC・他VCとの比較で「複数VCが語り始めたテーマ」を検出
