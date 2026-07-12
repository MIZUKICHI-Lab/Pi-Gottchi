# ハードウェア連動

## 方針

Pi-Gottchiの実行中プロセスだけが、名前付きの安全なイベントをハードウェアへ渡します。モデルに任意角度、GPIO番号、shellコマンドは渡しません。

```text
ボタン / IMU / バッテリー / 会話状態
                ↓
          chara.py のイベント
          ├─ 表情・LED・音声
          ├─ アプリ内スリープ
          └─ ServoAnimator → hardware PWM → SG90
```

サーボ処理は専用スレッドで動き、描画や音声通信を止めません。要求を溜め込まず常に最新のリアクションを優先し、設定した角度範囲を越えず、数度ずつ動かします。PWM初期化や動作に失敗した場合はサーボだけを無効化し、画面と会話は継続します。

## リアクションと動き

| 状態 | 動作 |
|---|---|
| `happy` | 小さく左右へ1往復 |
| `excited` | 少し大きく左右へ1往復 |
| `surprised` / `wake` | 片側へ傾いて中央へ戻る |
| `eating` | 小さく2回動いて中央へ戻る |
| `talking` | 応答再生中にだけ小さく左右へ動く |
| `dizzy` | 揺らされた直後なので追加で振らず中央へ戻る |
| `sleeping` | `SERVO_SLEEP_ANGLE` の安全姿勢へ移る |

サーボが動いている間はLiveマイク送信を止め、機械音を発話として拾わないようにします。`listening` と `thinking` では動かしません。

## 有効化と校正

初期状態は無効です。[配線ガイド](../app/WIRING.md)に従って外部電源と共通GNDを準備し、ホーンや負荷を外して次を実行します。

```bash
cd ~/Pi-Gottchi/app
sudo python3 servo.py
```

中央90度から75度、105度へ動いて中央へ戻り、端で唸らないことを確認します。その後 `app/.env` に設定します。

```dotenv
SERVO_ENABLED=1
SERVO_CHANNEL=0
SERVO_MIN_ANGLE=70
SERVO_CENTER=90
SERVO_MAX_ANGLE=110
SERVO_SLEEP_ANGLE=80
SERVO_MIN_PULSE_US=500
SERVO_MAX_PULSE_US=2400
SERVO_REVERSED=0
SERVO_HOLD=0
```

まずは `SERVO_MIN_ANGLE` と `SERVO_MAX_ANGLE` の狭い範囲で試します。方向が逆なら配線を変えず `SERVO_REVERSED=1` にします。SG90を交換した場合はパルス幅も再校正してください。

角度やパルス幅に数値でない値、`nan`、`inf` が入った場合は安全な既定値へ戻し、それでも範囲が不正ならサーボだけを無効化します。

`SERVO_HOLD=0` は動作後にPWMを止め、発熱、消費電力、唸りを抑えますが、保持トルクも失います。重心機構が受動的に安定しない場合だけ `1` を検討し、温度、電源電流、連続運転を実機で確認します。

## 音声スリープ

確定した1ターン全体が次のような直接的な別れ・就寝発話だった場合、返答再生後にアプリ内スリープへ入ります。

- おやすみ / おやすみなさい
- もう寝るね / 寝て / スリープして
- さよなら / バイバイ / またね / じゃあね

「おやすみってどういう意味」「まだ寝ない」「さよならを英語で何と言う」のような文は実行しません。誤認識を考慮し、音声からOS shutdownは行いません。shutdownは従来どおりボタン10秒長押しと電池保護だけです。

睡眠時はLive WebSocketと連続録音を停止し、LCDバックライトを `SLEEP_BACKLIGHT` まで下げ、サーボを睡眠姿勢へ移します。Pi Zero WのバックライトはON/OFF式なので、明示設定がなければ0で消灯します。PWM対応機では既定12%まで減光します。ボタン、揺れ、充電開始、一定以上の環境音で起床し、画面とLiveを戻します。環境音の確認は3秒ごとの短い録音なので、確実な起床手段はボタンまたはIMUです。

## 重心の閉ループ制御について

今回は安全な範囲のオープンループ動作までです。現在の `ShakeMonitor` は揺れイベントだけを公開し、姿勢の連続値は制御へ渡していません。閉ループ重心制御には、最新XYZ値、ローパスフィルタ、デッドバンド、角速度上限、センサー途絶時の中央復帰、機械ストッパー、筐体ごとのゲイン校正が必要です。

H3LIS331DLは高衝撃測定向けです。微細な傾きを使う段階では、姿勢制御に適したセンサーと機構を決めてから別フェーズで実装します。

## OpenClaw / OpenCodeを入れない理由

今回の実機は32bit ARMv6、メモリ約427MiBのRaspberry Pi Zero Wです。

- [OpenClawのRaspberry Pi公式ガイド](https://docs.openclaw.ai/install/raspberry-pi)は64bit OSと最低1GB RAMを前提とし、Zero系を非推奨としています。
- [OpenCode公式インストーラー](https://opencode.ai/install)のLinux配布対象はx64/arm64で、armv6は対象外です。
- OpenClawは常駐パーソナルAI、OpenCodeは開発用コーディングエージェントで、既存の音声・記憶・daemonと役割が重複するか用途が異なります。
- [OpenClawのセキュリティ資料](https://docs.openclaw.ai/gateway/security)と[OpenCodeのSecurity方針](https://github.com/anomalyco/opencode/security)はいずれも、強いツール権限を与える際の隔離・運用上の注意を示しています。rootで動くGPIO端末へ汎用shellエージェントを常駐させません。

より曖昧な自然言語で家電等を操作する段階では、まず[Gemini LiveのFunction Calling](https://ai.google.dev/gemini-api/docs/live-api/tools)へ `sleep()` や `set_pose(name)` のような少数の可逆な許可済み関数だけを公開します。メール、カレンダー、ブラウザまで扱う場合に限り、OpenClawをPi 4/5、NAS、PCなど別ホストで動かし、Pi-Gottchiには認証付きの狭いAPIだけを見せる構成を検討します。

なおChatGPT Proの契約に一般API利用料は含まれません。[OpenAI公式ヘルプ](https://help.openai.com/en/articles/9793128/)のとおりAPIは別会計です。現在のGemini Live直結は追加エージェントを挟まず、[Gemini APIの無料枠](https://ai.google.dev/gemini-api/docs/pricing)をそのまま利用できます。

## 一次資料

- [Raspberry Pi GPIO公式資料](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio-and-the-40-pin-header)
- [Raspberry Pi pwm-2chan overlay](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README)
- [Linux Kernel PWM sysfs仕様](https://docs.kernel.org/driver-api/pwm.html#using-pwms-with-the-sysfs-interface)
- [TowerPro SG90公式仕様](https://towerpro.com.tw/product/sg90-analog/)
- [Whisplay公式ハードウェア資料](https://docs.pisugar.com/docs/product-wiki/whisplay/overview#hardware-resources)
