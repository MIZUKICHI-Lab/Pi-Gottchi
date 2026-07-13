# 音声パイプライン設計

## 現在の流れ

```text
Whisplayマイク
  → arecord (16kHz / S16LE / mono)
  → 100ms PCMチャンク
  → Gemini Live WebSocket
  → サーバー側VAD（無音700msで発話終了）
  → 24kHz PCM応答
  → aplay
```

応答音声、定型ボイス、サーボ動作中は半二重になり、マイクから読んだデータを送信しません。停止が1秒を超えた時だけ `audioStreamEnd` を送り、サーバー側に残った入力を確定させます。短い停止では送らないため、文中の発話を誤ってflushしません。再生終了後は既定350msの残響待ちを置いてから送信を再開します。

Gemini側は `NO_INTERRUPTION` とし、応答再生中も安全な半二重を維持します。内蔵AECがない筐体で再生中の割り込みを有効にすると、スピーカー音を本人の発話と誤検出し、順序非保証の字幕も前後のターンへ混ざる可能性があるためです。返答を原則1短文へ制限し、半二重で聞けない時間そのものを短くします。完全な割り込み会話には、受信と再生のキュー分離に加えてAECまたは同等のエコー対策が必要です。

## 文字起こしを制御信号にしない理由

Gemini Live API の `inputTranscription` と `outputTranscription` は、モデル音声など他のサーバーメッセージとは独立して配送され、順序は保証されません。そのため次の用途には使いません。

- マイク音声の送信を止める時刻
- 返答音声を再生するかどうか
- 発話が終わったことを知らせるチャイム
- `interrupted` や `turnComplete` の処理を省略する条件

文字起こしは字幕、ログ、記憶と、`turnComplete` 後の安全なローカルコマンド判定だけに使います。返答音声が字幕より先に届いても、そのまま全量を再生します。

聞き取り中の `inputTranscription` は「あなた: …」として画面へ逐次表示します。これは認識開始をすぐ確認するための表示専用で、部分字幕から睡眠などの操作は実行しません。ログには入力字幕の初回到着から応答音声開始までの秒数も記録します。

`turnComplete` の後も字幕が遅れて届き得るため、最大1.2秒の範囲で、最後の字幕から400ms待ってターンを確定します。この間は `think` 状態として新しい音声送信と自動睡眠を止めます。睡眠・切断が先に起きた場合は世代番号で古いタイマーを無効化し、前の音声コマンドを次の起床後へ持ち越しません。

## VADと遅延の設定

| 環境変数 | 既定値 | 許容範囲 | 意味 |
|---|---:|---:|---|
| `LIVE_CHUNK_MS` | 100 | 40〜500ms | 1回に送る入力音声。小さいほど固定遅延は減るがCPU・通信回数は増える |
| `LIVE_SILENCE_MS` | 700 | 500〜2000ms | 発話終了とみなす連続無音。小さすぎると文中の間で分割される |
| `LIVE_ECHO_GUARD_MS` | 350 | 100〜1000ms | 再生・サーボ動作後に残響を捨てる時間 |

Google は自動VADの `silenceDurationMs` に500〜800msを推奨し、約800msが内部既定値だと説明しています。本実装は日本語の自然な間と応答速度の中間として700msを採用しています。

公式Cookbookは16kHz入力を1024サンプル、約64msずつ送ります。Pi Zero Wは1コアのため、本実装は少し大きい100msを既定にしました。実機負荷が高い場合は、まず200msへ上げて比較します。500msを超える値にはできません。

## 声紋ライト

`VOICEPRINT` の既定は `off` です。Pi Zero WでのMFCC計算は応答前に実行すると目に見える遅延になるため、`log` は返答再生後の別スレッドで処理します。`gate` だけは応答前に同期判定する実験モードです。本人確認には使えません。

## Live切断時のREST経路

ボタンを押した瞬間にLiveが未接続なら、その押下は最後までREST経路へ固定されます。録音中にLiveが再接続しても録音を捨てず、RESTの聞き取り・返答・再生が終わるまでLiveを停止します。短押しの場合も、録音プロセスと定型反応が終わってからLiveを戻します。

## 検証

ハードウェアなしの回帰テストは次で実行できます。

```bash
python3 -m unittest discover -s tests -v
```

主に次を固定しています。

- 字幕更新が止まってもマイクをミュートしない
- 返答音声が入力字幕より先でも再生する
- `interrupted` と `turnComplete` が同じメッセージでも完了処理する
- `turnComplete` より後の字幕を同じターンへ結合し、grace中の睡眠・次入力を防ぐ
- sleep/切断時に古い字幕確定タイマーとコマンドを破棄する
- 起床操作と字幕確定が競合しても、古いスリープ命令を復活させない
- suspend/reconnect中の旧録音チャンクを新セッションへ送らない
- 応答音声の再生終了待ち中でも、suspendから`aplay`を停止できる
- `audioStreamEnd` を1秒未満の短いミュートでは送らない
- VAD・チャンク設定を安全範囲へ制限する
- 押下開始時に選んだREST/Live経路を解放時まで保持する

## 公式資料

- [Gemini Live WebSocket API reference](https://ai.google.dev/api/live)
- [Live API capabilities — VAD parameters](https://ai.google.dev/gemini-api/docs/live-api/capabilities#understanding-vad-parameters-and-their-impact-on-quality)
- [Live API best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices)
- [Google Gemini公式Live API Cookbook](https://github.com/google-gemini/cookbook/blob/main/quickstarts/Get_started_LiveAPI.py)
