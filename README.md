# Pi-Gottchi「もこ」

Raspberry Pi Zero W と PiSugar Whisplay HAT で動く、小さな音声対話キャラクターです。
話しかけると音声で返事をし、ボタン、揺れ、周囲の音、充電状態に応じて表情や反応が変わります。

![もこのプレビュー](preview.png)

## 主な機能

- Gemini Live API を使ったハンズフリー音声対話
- 240 × 280 LCD 上の表情・まばたき・字幕アニメーション
- 名前、好み、会話トピックを端末内に保存する記憶機能
- ボタン操作、環境音、充電状態へのリアクション
- 表情・会話・睡眠に連動する安全範囲のサーボ動作（任意）
- 「おやすみ」「さよなら」等によるマイク停止・画面減光のソフトスリープ
- MPU6050、ADXL345、H3LIS331DL による揺れ検知（任意）
- systemd による自動起動と異常終了時の再起動
- APIを使えない場合の限定的なオフライン動作

## 必要なもの

| 種別 | 内容 |
|---|---|
| 本体 | Raspberry Pi Zero W または Zero 2 W |
| HAT | PiSugar Whisplay HAT |
| OS | Raspberry Pi OS |
| ネットワーク | Gemini 音声対話を使う場合に必要 |
| API | Google Gemini API キー |
| 任意 | PiSugar バッテリー、対応加速度センサー、SG90 サーボ |

Whisplay HAT には LCD、マイク、スピーカー、ボタン、RGB LED が含まれます。

## はじめる

1. Raspberry Pi OS と Whisplay の公式ドライバーを準備します。
2. このリポジトリを Raspberry Pi にクローンします。
3. Python・音声関連の依存パッケージを導入します。
4. `app/.env.example` を `app/.env` にコピーし、Gemini API キーを設定します。
5. `app/chara.py` を起動します。

詳しいコマンド、systemd 登録、動作確認は [セットアップガイド](docs/SETUP.md) を参照してください。

## 操作

| 操作 | 動作 |
|---|---|
| 起きている「もこ」に話しかける | 発話を自動検出して音声で返答 |
| ボタンを1回押す | なでる |
| ボタンを2回押す | 軽い揺れを模擬（動作確認用） |
| ボタンを3回以上押す | 強い揺れを模擬（動作確認用） |
| Live 接続中以外にボタンを押しながら話す | 録音後、REST API 経由で返答 |
| ボタンを10秒間押す | 安全にシャットダウン |
| 本体を揺らす | 驚く、または目を回す（センサー接続時） |
| 充電器を接続する | 食事を模した反応と残量表示 |

放置すると眠り、ボタン、揺れ、または一定以上の音で起きます。詳しくは[使い方](docs/USAGE.md)を参照してください。

サーボは安全のため初期状態では無効です。外部電源、配線、無負荷校正後に有効化します。詳しくは[ハードウェア連動](docs/HARDWARE_INTEGRATION.md)を参照してください。

## ドキュメント

- [セットアップ](docs/SETUP.md) — インストール、APIキー、systemd
- [使い方](docs/USAGE.md) — 操作、表情、保存データ
- [設定とカスタマイズ](docs/CONFIGURATION.md) — 環境変数、性格、感度、モデル
- [ハードウェアと配線](app/WIRING.md) — センサー、サーボ、GPIO
- [アーキテクチャ](docs/ARCHITECTURE.md) — 構成、音声処理、フォールバック
- [音声パイプライン](docs/AUDIO_PIPELINE.md) — VAD、遅延、イベント順、回帰テスト
- [ハードウェア連動](docs/HARDWARE_INTEGRATION.md) — サーボ、音声スリープ、AI基盤の判断
- [トラブルシューティング](docs/TROUBLESHOOTING.md) — ログ確認と代表的な問題
- [コントリビューションガイド](CONTRIBUTING.md) — 開発への参加方法
- [セキュリティ](SECURITY.md) — APIキー、音声・会話データの扱い

## プロジェクト構成

```text
app/              Raspberry Pi 上で動くアプリケーション
boot_frames/      起動アニメーション用フレーム
docs/             利用者・開発者向けドキュメント
preview.py        顔を実機なしで画像出力するプレビュー
gen_boot_frames.py
                  起動フレーム生成ツール
```

## プライバシーと費用

音声対話と記憶の要約には Gemini API を使用します。利用条件、料金、無料枠、データの取り扱いは変更される可能性があります。Google の最新の規約と管理画面を確認し、機密情報や第三者の音声を不用意に送信しないでください。詳細は[セキュリティ](SECURITY.md)にまとめています。

## ライセンス

現時点ではライセンスファイルが含まれていません。そのため、公開リポジトリであっても、第三者に複製・改変・再配布を許可するオープンソースライセンスは付与されていません。利用条件を明確にするには、プロジェクト所有者が MIT、Apache-2.0 など適切なライセンスを選び、`LICENSE` を追加してください。

## 謝辞

- [PiSugar Whisplay HAT](https://docs.pisugar.com/docs/product-wiki/whisplay/overview)
- [Google Gemini API](https://ai.google.dev/gemini-api/docs)
- Open JTalk（任意のローカル音声フォールバック）
