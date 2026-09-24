# a16z Digest — Claude Code 向け作業メモ

## 目的
a16zの発信（発言）とポートフォリオ（行動）、科学・工学メディアを収集し、
関心トラックごとのタブとつながりグラフを持つ週次サイトを生成する個人用ツール。
使い道は、世の中の需要の把握と、今後伸びる技術・企業の先取り。

## 構成
- `digest.py`：収集 → LLM構造化 → トレンド集計 → ポートフォリオ差分 → 編集 → サイト生成 → 通知メール
- `config.yaml`：ソース、関心トラック、読者像、ポートフォリオのセレクタ
- `site_template.html`：タブ、詳細表示、d3のつながりグラフ（`__DATA__`にJSONが入る）
- `.github/workflows/digest.yml`：毎日収集、月曜に本処理とPagesへのデプロイ
- `data/`：SQLite（items / keywords / predictions / portfolio）と週ごとの号のJSON

## 本実装で最初にやること（デモでは未検証）
0. **GitHubリポジトリの作成から始める**
   - `git init` し、`.gitignore` を作る（`site/`、`out/`、`__pycache__/`、`.env`、将来の保有銘柄リストなど個人情報のファイル）
   - `gh repo create a16z-digest --private --source=. --push` でリポジトリを作成してpushする（ghが未ログインなら `gh auth login` から）
   - 公開範囲はユーザーに確認する。privateでGitHub Pagesを使うには有料プラン（Pro以上）が必要。
     無料で運用するならpublicにするか、Cloudflare Pagesなど別のホスティングに切り替える
   - この時点ではSecretsやPagesの設定はしない（手順5で行う）
1. **フィードURLの検証**：config.yamlの全フィードを取得し、件数・日付・本文の有無を確認。
   American DynamismのSubstack URLは a16z.com/newsletters から特定して追加する
2. **ポートフォリオページの構造確認**：a16z.com/portfolio のHTMLを取得し、
   `item_selector` / `name_selector` / `sector_selector` を合わせる。
   JSで描画されている場合は、ページが読み込むJSON APIを探すか、Playwrightで取得する方式に変える
3. **初回実行**：`python digest.py --backfill-days 35 --dry-run` で `site/index.html` を確認。
   トラックの振り分けやグラフの密度が不自然なら、抽出と編集のプロンプトを調整する
4. **APIコストの確認**：記事数×抽出1回＋編集1回。週あたりの件数を見て `max_chars` を調整
5. **GitHub設定**：Secrets、Pagesの有効化、`site_url` の設定、workflow_dispatchでの疎通確認

## 方針
- 投資先の判定はLLMの推測よりポートフォリオページの実データを優先する
- a16zはポジショントークを含む前提で、工学メディアを裏取りに使う
- ソースを増やす前に、週報の量とノイズを確認する（第2段階: MONOist、Hackaday、arXiv cs.RO / eess.SY）
