"""端末内で安全に処理する、少数の音声コマンド判定。

モデルの部分文字起こしではなく、確定した1ターンだけを渡すことを前提にする。
誤認識で危険な操作をしないよう、ここでは可逆なアプリ内スリープだけを扱う。
"""
import re
import unicodedata


_IGNORED_RE = re.compile(r"[\s、。,.!！?？…・~〜]+")
_SLEEP_RE = re.compile(
    r"^(?:(?:ねえ|ねぇ)?もこ(?:ちゃん)?(?:は)?)?"
    r"(?:(?:じゃあ|それじゃあ))?"
    r"(?:"
    r"お(?:やすみ|休み)(?:なさい)?(?:また(?:あした|明日))?"
    r"|(?:きょうは|今日は)?(?:もう)?(?:ねる|寝る)(?:ね|よ)?"
    r"|(?:もう)?(?:ね|寝)(?:て|よう)(?:ね|よ)?"
    r"|スリープ(?:して)?(?:ね)?"
    r"|(?:さよなら|さようなら|ばいばい|バイバイ|またね|じゃあね|じゃね)"
    r"(?:また(?:あした|明日))?"
    r")$"
)


def normalize_utterance(text):
    """表記ゆれと発話中の区切り記号だけを正規化する。"""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return _IGNORED_RE.sub("", normalized)


def detect_control_intent(text):
    """確定発話が端末コマンドなら名前を返す。それ以外は ``None``。"""
    normalized = normalize_utterance(text)
    if normalized and _SLEEP_RE.fullmatch(normalized):
        return "sleep"
    return None
