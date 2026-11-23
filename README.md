# AI生成画像 メタデータ管理ツール

AI生成画像（PNG / WebP / JPEG）のメタデータから特定タグのみを除去、またはメタデータ全体を完全消去するためのツールセットです。確認用のメタデータ書き出しスクリプトも同梱しています。

## ファイル構成

| ファイル名 | バージョン | 役割 |
| :--- | :--- | :--- |
| `clean_tags.py` | v2.1 | 指定タグのみを除去、またはメタデータ全体を完全消去する |
| `dump_meta.py` | v1.0 | 画像内のメタデータ（テキスト / Exif / ステルス情報）をテキストファイルに書き出す |
| `requirements.txt` | - | 必要なPythonパッケージ一覧 |
| `LICENSE` | - | ライセンス情報（MIT License） |

## 対応フォーマット・動作確認済みツール

- **ComfyUI（PNG / WebP）**: `prompt` / `workflow`（JSON） ※動作確認済み
- **NovelAI V4 / V5（PNG / WebP）**: `Description` / `Comment`（JSON）およびアルファチャンネルのステルス情報（`stealth_pngcomp` / `stealth_pnginfo`） ※動作確認済み
- **AUTOMATIC1111 / Forge / SD.Next（PNG / WebP / JPEG）**: `parameters` 内のプロンプト部分（`Steps:` 以降の設定行は保護）

## 動作環境・事前準備

Python 3.8以上が導入されていることを前提とします。以下のコマンドで必要なパッケージをインストールしてください。

```bash
pip install -r requirements.txt
```

（直接インストールする場合は `pip install Pillow numpy piexif` でも同じです）

---

## 1. `clean_tags.py` の使い方

### 主な機能

- **指定タグのみ除去（デフォルトモード）**:
  - 元のタグ表記を指定するだけで、スペースとアンダースコアの違い（`tag_name` / `tag name`）、括弧のエスケープ有無（`tag_(1)` / `tag_\(1\)`）、重み付け表記（`tag:1.2` 等）、NovelAI V4/V5の `artist:` プレフィックスおよび数値重み記法（`2.0::...::`）を自動でマッチ・除去します。
  - タグ除去後に残る連続カンマ、空括弧（`()`, `{}`, `[]`）、空の数値重みブロック（`2.0::::`）、全角空白を自動で整形します。
  - 外部のテキストファイル（`-f`）から除去タグリストを読み込めるため、スクリプト本体を書き換えずに運用できます。
- **メタデータ完全消去モード（`-s` / `--strip-all`）**:
  - PNGのテキスト系チャンク（`tEXt` / `iTXt` / `zTXt` / `eXIf`）、WebP・JPEGのExif、NovelAIのステルス情報をすべて完全消去します。
  - PNGの色管理チャンク（`iCCP` / `sRGB` 等）や、NovelAI V5の**透過領域（アルファチャンネル）は維持**されます。
- **安全機構**:
  - `-o`（`--output`）を指定すると、元ファイルを上書きせず別ディレクトリへ出力します（`-r` 指定時はサブディレクトリ構造も維持）。
  - 実行前に処理対象・出力先・内容を表示する確認プロンプト（`[y/N]`）を表示します（`--dry-run` 時や `-y` 指定時はスキップ）。
  - 一時ファイル経由でアトミックに書き出し、変更がないファイルは上書きしません。

### 除去対象タグの設定

除去対象タグは以下のいずれかの方法で指定できます（優先順位：`-f` または `-t` ＞ スクリプト内 `DEFAULT_TARGET_TAGS`）。

1. **外部タグファイルで指定（おすすめ・`-f`）**
   1行に1タグを記述したUTF-8テキストファイル（例：`tags.txt`）を用意します。`#` で始まる行はコメントとして無視されます。
   ```text
   # 除去したい作家名・絵柄タグ
   artist_tag_1
   artist_tag_(2)
   ```
2. **スクリプト内に記述（デフォルト）**
   `clean_tags.py` 内の `DEFAULT_TARGET_TAGS` リストに記述します。
   ```python
   DEFAULT_TARGET_TAGS = [
       "artist_tag_1",
       "artist_tag_(2)",
   ]
   ```

> **補足**: 実行時に `-t`（`--tags`）または `-f`（`--tags-file`）を指定した場合、`DEFAULT_TARGET_TAGS` の内容は**無視され**、指定したタグのみが除去対象となります。

### 実行コマンド例

```bash
# 単一ファイルから指定タグのみ除去する（元ファイルを上書き）
python clean_tags.py -i image.png

# 外部タグファイルを使い、別ディレクトリへ出力する（元ファイルは上書きしない）
python clean_tags.py -i ./image_dir -r -f tags.txt -o ./output_dir

# ファイルを書き換えずに更新対象のみ確認する（ドライラン・確認プロンプトなし）
python clean_tags.py -i ./image_dir -r -f tags.txt --dry-run

# コマンドラインから除去タグを直接指定する（DEFAULT_TARGET_TAGS は無視）
python clean_tags.py -i image.png -t artist_tag_1 "artist_tag_(2)"

# 単一ファイルのメタデータを完全消去する（ステルス情報含む・透過維持）
python clean_tags.py -i image.png -s

# ディレクトリ内のメタデータを別フォルダへ完全消去して書き出す（確認プロンプトをスキップ）
python clean_tags.py -i ./image_dir -r -s -o ./output_dir -y
```

### オプション一覧

| オプション | 説明 |
| :--- | :--- |
| `-i`, `--input` | （必須）処理対象の画像ファイル、またはディレクトリのパス |
| `-o`, `--output` | 出力先ディレクトリ（省略時は元ファイルを上書き） |
| `-r`, `--recursive` | ディレクトリ指定時にサブディレクトリも再帰的に処理する |
| `-t`, `--tags` | 除去するタグをスペース区切りで指定。**指定時は `DEFAULT_TARGET_TAGS` は無視されます**（`-f` / `-s` と同時指定不可） |
| `-f`, `--tags-file` | 除去するタグを1行ずつ記述したUTF-8テキストファイルを指定（`#` 始まりはコメント行、`-t` / `-s` と同時指定不可） |
| `-s`, `--strip-all` | メタデータ（テキストチャンク / Exif / ステルス情報）をすべて完全消去する（`-t` / `-f` と同時指定不可） |
| `--dry-run` | 実際の書き換えを行わず、更新予定のファイルと対象領域のみ表示する（確認プロンプトなし） |
| `-y`, `--yes` | 実行前の確認プロンプト（`[y/N]`）をスキップする |

---

## 2. `dump_meta.py` の使い方

画像のメタデータが正しく除去・消去されているかを確認するための補助スクリプトです。

### 出力仕様

- 元画像と同じディレクトリに `<ファイル名>_<拡張子>_meta.txt`（例：`image.png` → `image_png_meta.txt`）をUTF-8で出力します。
- JSON形式のメタデータは読みやすくインデント整形されます。
- メタデータが一切見つからない画像はファイルを出力せず、コンソールに `メタデータなし: <ファイルパス>` のみ表示します。

### 実行コマンド例

```bash
# 単一画像のメタデータを書き出す
python dump_meta.py -i image.png

# ディレクトリ直下の画像のメタデータを一括で書き出す
python dump_meta.py -i ./image_dir

# サブディレクトリも含めて再帰的に書き出す
python dump_meta.py -i ./image_dir -r
```

---

## 推奨ワークフロー

### 通常運用（別ディレクトリ出力で元画像を保護）

```bash
# 1. ドライランで対象ファイルを確認
python clean_tags.py -i ./image_dir -r -f tags.txt --dry-run

# 2. 別ディレクトリへ出力（元ファイルは上書きしない）
python clean_tags.py -i ./image_dir -r -f tags.txt -o ./output_dir

# 3. 必要に応じて出力側のメタデータを書き出して確認
python dump_meta.py -i ./output_dir -r
```

### 慎重に確認したい場合（初回・タグ追加時・別ツールの画像を初めて扱う場合）

`-o` で別ディレクトリへ出力したあと、原本側と出力側の双方で `dump_meta.py` を実行し、出力された `*_meta.txt` の差分を比較します。

```bash
python clean_tags.py -i ./image_dir -r -f tags.txt -o ./output_dir
python dump_meta.py -i ./image_dir -r
python dump_meta.py -i ./output_dir -r
```

## 注意事項

- `-o` を省略した場合は元ファイルを直接上書きします。大量の画像を処理する前は `-o` の併用か `--dry-run` での事前確認を推奨します。
- NovelAIのステルス情報を編集または消去した場合、公式の改ざん検知用署名（`signed_hash`）とは一致しなくなります（一般的なメタデータ閲覧ツールでの表示には影響ありません）。

## ライセンス

このプロジェクトは [MIT License](LICENSE) のもとで公開されています。

## 更新履歴

### 2026-11-23
- **`clean_tags.py` v2.1**
  - 外部タグリストファイル読み込みオプション（`-f` / `--tags-file`）を追加
  - 別ディレクトリ出力オプション（`-o` / `--output`）を追加（元ファイルの上書きを回避可能）
  - NovelAI V4/V5 の `artist:` プレフィックスおよび数値重み記法（`2.0::...::`）の自動整形・空ブロック削除に対応
- **`clean_tags.py` v2.0**
  - メタデータ完全消去モード（`-s` / `--strip-all`）を追加（PNGの色管理チャンクおよびNovelAI V5の透過領域を維持したまま完全消去に対応）
  - 実行前の確認プロンプト（`[y/N]`）とスキップ用オプション（`-y` / `--yes`）を追加
  - `-t` と `-s` の排他制御を追加
- **`dump_meta.py` v1.0**
  - 新規作成（PNG / WebP / JPEGのテキストチャンク・Exif・ステルス情報を一括書き出し）

### 2026-11-22
- **`clean_tags.py` v1.0**
  - 初版作成（ComfyUI / NovelAI / A1111・Forge 形式のPNG・WebP・JPEGに対する指定タグ除去機能）
