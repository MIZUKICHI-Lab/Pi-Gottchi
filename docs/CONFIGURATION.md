# 設定とカスタマイズ

## 環境変数

設定ファイルは `app/.env` です。`KEY=value` 形式で記述します。

| 変数 | 必須 | 既定値 | 用途 |
|---|---:|---|---|
| `GEMINI_API_KEY` | 会話時のみ | なし | Gemini API の認証 |
| `GEMINI_MODEL` | いいえ | `gemini-2.5-flash` | REST 会話モデル |
| `GEMINI_LIVE_MODEL` | いいえ | `gemini-2.5-flash-native-audio-latest` | Live 音声モデル |
| `GEMINI_TTS_MODEL` | いいえ | コード内の候補 | 反応ボイス生成モデル |
| `LIVE_CHUNK_MS` | いいえ | `100` | Liveへ送る音声チャンク長（40〜500ms） |
| `LIVE_SILENCE_MS` | いいえ | `700` | 発話終了とみなす無音（500〜2000ms） |
| `LIVE_ECHO_GUARD_MS` | いいえ | `350` | 再生・サーボ後の残響破棄（100〜1000ms） |
| `VOICEPRINT` | いいえ | `off` | `off`、`log`、`gate` のいずれか |
| `AWAKE_BACKLIGHT` | いいえ | `100` | 起床中のLCDバックライト（0〜100） |
| `SLEEP_BACKLIGHT` | いいえ | `12` | 睡眠中のLCDバックライト（0〜100） |

モデル名は API 側で変更・廃止されることがあります。接続できない場合は Google の最新ドキュメントで利用可能なモデル名を確認してください。

## 声紋ライト

声紋ライトは簡易的な音声特徴比較であり、本人確認やセキュリティ機能ではありません。

| 値 | 動作 |
|---|---|
| `off` | 無効化 |
| `log` | 類似度をログへ出力するだけ |
| `gate` | しきい値未満の声を処理しない実験モード |

`log` は返答再生後に別スレッドで計算します。`gate` は返答前に同期計算するためPi Zeroでは遅延が増え、周囲の雑音、マイク位置、声質の変化でも誤判定します。通常は `off` を推奨します。しきい値は `app/live.py` の `VP_THRESH` です。

## サーボ

サーボは安全のため既定で無効です。配線、外部電源、無負荷テスト後に設定します。

| 変数 | 既定値 | 用途 |
|---|---:|---|
| `SERVO_ENABLED` | `0` | `1` でリアクション連動を有効化 |
| `SERVO_CHANNEL` | `0` | PWM channel。0=BCM12、1=BCM13 |
| `SERVO_MIN_ANGLE` | `70` | アプリが使う安全角度の下端 |
| `SERVO_CENTER` | `90` | 中央・重心中立姿勢 |
| `SERVO_MAX_ANGLE` | `110` | アプリが使う安全角度の上端 |
| `SERVO_SLEEP_ANGLE` | `80` | 睡眠姿勢 |
| `SERVO_MIN_PULSE_US` | `500` | 0度相当の校正パルス幅 |
| `SERVO_MAX_PULSE_US` | `2400` | 180度相当の校正パルス幅 |
| `SERVO_REVERSED` | `0` | `1` でリアクション方向を反転 |
| `SERVO_HOLD` | `0` | `1` で動作後も保持トルクを維持 |

設定と故障安全性は[ハードウェア連動](HARDWARE_INTEGRATION.md)と[配線ガイド](../app/WIRING.md)を先に確認してください。

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
