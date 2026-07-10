# 設定とカスタマイズ

## 環境変数

設定ファイルは `app/.env` です。`KEY=value` 形式で記述します。

| 変数 | 必須 | 既定値 | 用途 |
|---|---:|---|---|
| `GEMINI_API_KEY` | 会話時のみ | なし | Gemini API の認証 |
| `GEMINI_MODEL` | いいえ | `gemini-2.5-flash` | REST 会話モデル |
| `GEMINI_LIVE_MODEL` | いいえ | `gemini-2.5-flash-native-audio-latest` | Live 音声モデル |
| `GEMINI_TTS_MODEL` | いいえ | コード内の候補 | 反応ボイス生成モデル |
| `VOICEPRINT` | いいえ | `log` | `off`、`log`、`gate` のいずれか |

モデル名は API 側で変更・廃止されることがあります。接続できない場合は Google の最新ドキュメントで利用可能なモデル名を確認してください。

## 声紋ライト

声紋ライトは簡易的な音声特徴比較であり、本人確認やセキュリティ機能ではありません。

| 値 | 動作 |
|---|---|
| `off` | 無効化 |
| `log` | 類似度をログへ出力するだけ |
| `gate` | しきい値未満の声を処理しない実験モード |

`gate` は周囲の雑音、マイク位置、声質の変化で誤判定する可能性があります。初めは `off` または `log` を推奨します。しきい値は `app/live.py` の `VP_THRESH` です。

## キャラクター設定

| 変更内容 | ファイル・定数 |
|---|---|
| 性格と口調 | `app/voice.py` の `SYSTEM_PROMPT` |
| TTS の話し方 | `app/voice.py` の `TTS_STYLE` |
| Gemini の声 | `app/live.py` の `VOICE`、`app/voice.py` の `GEMINI_VOICE` |
| 定型リアクション | `app/voice.py` の `CLIPS` |
| 表情と描画 | `app/face.py` |

`CLIPS` や音声スタイルを変更した場合、既存の `app/voices/` を削除すると次回起動時に音声が再生成されます。API利用量が発生し得るため注意してください。

## 動作しきい値

| 変更内容 | ファイル・定数 |
|---|---|
| 昼・夜の睡眠時間 | `app/chara.py` の `SLEEP_AFTER`、`NIGHT_SLEEP_AFTER` |
| 夜間の範囲 | `app/chara.py` の `NIGHT` |
| 環境音・起床音量 | `app/chara.py` の `AMBIENT_RMS`、`WAKE_RMS` |
| 低残量・終了残量 | `app/chara.py` の `BATT_LOW`、`BATT_CRITICAL` |
| 揺れ感度 | `app/imu.py` の `LIGHT`、`HARD` |
| 声紋しきい値 | `app/live.py` の `VP_THRESH` |

値を変更したらサービスを再起動します。

```bash
sudo systemctl restart moko.service
```

## ハードウェア固有の設定

- Whisplay ランタイムのパス: `app/chara.py` と `app/splash.py`
- ALSA デバイス: `app/chara.py` の `ALSA_DEV`
- I2C バス: `app/imu.py` の `I2C_BUS`
- サーボPWM: `app/servo.py` と `/boot/firmware/config.txt`

パスやデバイス名は Raspberry Pi の構成によって異なります。変更前に `arecord -l`、`aplay -l`、`i2cdetect -l` で実機の値を確認してください。
