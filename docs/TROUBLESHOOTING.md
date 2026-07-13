# トラブルシューティング

## 最初に確認すること

```bash
sudo systemctl status moko.service
sudo journalctl -u moko.service -n 100 --no-pager
arecord -l
aplay -l
```

リアルタイムログは次のコマンドで確認できます。

```bash
sudo journalctl -u moko.service -f
```

## 起動しない

### `ModuleNotFoundError: whisplay`

Whisplay の公式ドライバーとランタイムを確認してください。`app/chara.py` と `app/splash.py` は既定で `/home/mizukichi/Whisplay/runtime` を参照します。ユーザー名や配置先が異なる場合はパスの変更が必要です。

### systemd では起動しないが手動実行できる

`moko.service` の `WorkingDirectory` と `ExecStart` が実際の `app` ディレクトリを指しているか確認します。修正後は再読込します。

```bash
sudo systemctl daemon-reload
sudo systemctl restart moko.service
```

### 画面が表示されない

- Whisplay のサンプルが動くか確認する
- `moko-splash.service` と `moko.service` のログを確認する
- HAT の向きと40ピンヘッダーの挿入状態を電源OFFで確認する

サービスが `active` でログに `expr=sleeping` が出る場合は睡眠中です。通常20秒、夜間10秒の無操作で自動睡眠しますが、この状態ではLive会話を維持しているため、そのまま話しかければ起きます。明示的な「おやすみ」後だけはLiveを停止するため、ボタンまたはIMUで起こします。Pi Zero Wは既定で睡眠顔を表示したままです。

## 音声対話が動かない

### APIキー未設定と表示される

`app/.env` に `GEMINI_API_KEY` があり、プレースホルダーのままではないことを確認します。ファイルは実行中の `chara.py` と同じディレクトリに置きます。

### Live セッションへ接続できない

- Raspberry Pi の時刻とインターネット接続を確認する
- API キーが有効か確認する
- `GEMINI_LIVE_MODEL` が現在利用可能なモデルか確認する
- API の割り当て、レート制限、請求設定を確認する

Live 接続が切れている間は、ボタンを押しながら話す REST フォールバックを試せます。

### 話しても反応しない

```bash
arecord -D plughw:CARD=whisplaysound -f S16_LE -r 16000 -c 1 -d 3 /tmp/test.wav
aplay -D plughw:CARD=whisplaysound /tmp/test.wav
```

録音に声が入っていなければ、Whisplay の音声ドライバーと ALSA デバイス名を確認します。環境のデバイス名が違う場合は `app/chara.py` の `ALSA_DEV` を変更してください。

### 自分の返答に自分で反応する

スピーカー音量を下げ、マイクとスピーカーの物理的な距離を取ります。コードは返答・定型音声・サーボ動作中と直後の入力を破棄しますが、強い残響がある筐体では完全に防げない場合があります。`LIVE_ECHO_GUARD_MS` を350から50〜100msずつ増やして確認します。

### 文中の間で聞き取りが終わる

現在の既定は `LIVE_SILENCE_MS=700` です。ゆっくり話す場合は800へ上げます。500未満は自然な間を発話終了と誤判定しやすいため設定できません。

```dotenv
LIVE_SILENCE_MS=800
```

以前の実装にあった、文字起こしが0.7秒更新されないと音声送信を止める処理と「聞こえた」チャイムは削除済みです。まだ発生する場合は、発生時刻と次のログを確認してください。

```bash
sudo journalctl -u moko.service --since "5 minutes ago" --no-pager
```

### 応答が遅い・CPU負荷が高い

Pi Zero Wで `LIVE_CHUNK_MS=100` が重い場合は200へ上げて比較します。`VOICEPRINT=gate` は応答前にCPU計算を行うため、通常は `off` にします。

```dotenv
LIVE_CHUNK_MS=200
VOICEPRINT=off
```

`rpi-connect signin --non-interactive` が繰り返し高CPUを使っている場合、音声処理ではなくRaspberry Pi Connectの認証再試行が単一CPUを圧迫しています。RPi Connectを使用しているか確認してから、再認証またはサービス停止を判断してください。無断で無効化すると別の遠隔接続手段を失う可能性があります。

## センサーが動かない

```bash
sudo i2cdetect -y 1
```

想定アドレス:

- H3LIS331DL: `0x18` または `0x19`
- ADXL345: `0x53`
- MPU6050系: 原則 `0x69`

表示されない場合は、電源を切ってから3.3V、GND、SDA、SCLを確認します。MPU6050 は PiSugar RTC との競合を避けるため AD0 を3.3Vへ接続してください。詳細は[配線ガイド](../app/WIRING.md)を参照してください。

## サーボで再起動する

電源不足の可能性が高い状態です。サーボには外部5V電源を使い、そのGNDと Raspberry Pi のGNDを共通にしてください。信号線を含む配線変更は電源OFFで行います。

### サーボが一度だけ動いて止まる

現行 `Servo.angle()` は `release()` 後にPWMを自動再有効化します。古い `servo.py` を直接配置していないか、Gitのコミットとサービスの `ExecStart` を確認してください。

```bash
git -C ~/Pi-Gottchi rev-parse --short HEAD
systemctl show moko.service -p ExecStart -p WorkingDirectory
```

### サーボの動作音で会話が反応する

アプリ経由の動作中はマイク送信を止めます。手動テストや外部回路で動かした音は検知されるため、サービスを停止して校正してください。`SERVO_HOLD=1` の連続的な唸りはミュート対象にできないため、端当たり、電源、機構を直すか保持を無効にします。

## バッテリー残量が表示されない

PiSugar が I2C で認識されているか確認してください。バッテリー未接続・非対応構成でも、それ以外の機能は動作します。

## ログを共有するとき

ログには会話の文字起こしが含まれることがあります。API キー、会話、ホスト名、ユーザー名などを削除してから Issue に貼り付けてください。
