# セットアップ

このガイドでは、Raspberry Pi OS を導入済みの Raspberry Pi Zero W / Zero 2 W に「もこ」をセットアップします。

## 1. Whisplay を準備する

PiSugar の[Whisplay ドキュメント](https://docs.pisugar.com/docs/product-wiki/whisplay/overview)に従い、HAT と公式ドライバーを準備してください。本アプリは、公式ランタイムが次の場所にある構成を前提にしています。

```text
/home/<ユーザー名>/Whisplay/runtime
```

現在の `app/chara.py` と `app/splash.py` は `/home/mizukichi/Whisplay/runtime` を参照します。別のユーザー名や配置先を使う場合は、この2ファイルのパスを環境に合わせて変更してください。

## 2. OS パッケージを導入する

```bash
sudo apt update
sudo apt install -y \
  python3-numpy python3-pil python3-requests python3-websockets \
  python3-smbus2 sox alsa-utils i2c-tools git
```

日本語表示用フォントがない場合は、VL Gothic も導入します。

```bash
sudo apt install -y fonts-vlgothic
```

Open JTalk は任意です。Gemini の音声生成を利用できないときのローカル音声フォールバックとして使います。利用する場合は Open JTalk 本体、辞書、対応する HTS voice を別途配置してください。

## 3. リポジトリを取得する

```bash
cd ~
git clone git@github.com:MIZUKICHI-Lab/Pi-Gottchi.git
cd Pi-Gottchi
```

HTTPS を使う場合は、clone URL を次に置き換えます。

```text
https://github.com/MIZUKICHI-Lab/Pi-Gottchi.git
```

## 4. API キーを設定する

[Google AI Studio](https://aistudio.google.com/apikey) で Gemini API キーを作成し、サンプル設定をコピーします。

```bash
cp app/.env.example app/.env
nano app/.env
chmod 600 app/.env
```

最低限、次の1行を設定します。

```dotenv
GEMINI_API_KEY=your_api_key_here
```

`.env` は `.gitignore` の対象です。API キーをコミット、スクリーンショット、ログへ含めないでください。

## 5. 手動で起動する

最初にハードウェア不要の回帰テストを実行します。

```bash
cd ~/Pi-Gottchi
python3 -m unittest discover -s tests -v
```

その後アプリを起動します。

```bash
cd ~/Pi-Gottchi/app
python3 chara.py
```

確認ポイント:

- LCD にキャラクターが表示される
- ボタンを1回押すと表情が変わる
- `arecord -l` と `aplay -l` に Whisplay のサウンドデバイスが表示される
- API キー設定時はログに会話プロバイダーと Live セッション接続が表示される

終了は `Ctrl+C` です。

## 6. systemd で自動起動する

同梱ユニットは `/home/mizukichi/Pi-Gottchi/app` を前提にしています。自分のユーザー名とクローン先に合わせて、コピー後のユニットを編集してください。

```bash
sudo cp app/moko.service /etc/systemd/system/
sudo cp app/moko-splash.service /etc/systemd/system/
sudo nano /etc/systemd/system/moko.service
sudo nano /etc/systemd/system/moko-splash.service
```

両ファイルの `/home/mizukichi/Pi-Gottchi/app` を、実際の `app` ディレクトリの絶対パス（例: `/home/pi/Pi-Gottchi/app`）へ置き換えます。その後、有効化します。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now moko-splash.service moko.service
sudo systemctl status moko.service
```

## 7. ログを確認する

```bash
sudo journalctl -u moko.service -f
```

起動できない場合は[トラブルシューティング](TROUBLESHOOTING.md)を参照してください。加速度センサーやサーボを追加する場合は[配線ガイド](../app/WIRING.md)へ進みます。

## 8. Gitで更新する

実行中ファイルを直接上書きせず、`main` をfast-forwardで取得してからサービスを再起動します。

```bash
cd ~/Pi-Gottchi
git pull --ff-only origin main
python3 -m unittest discover -s tests -v
sudo systemctl restart moko-splash.service moko.service
sudo systemctl status moko.service --no-pager
```

`app/.env`、`memory.json`、`state.json`、`voices/` はGit管理外なので、pullしても保持されます。
