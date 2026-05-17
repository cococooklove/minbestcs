"""
리뷰 분류 및 AI 답변 생성 (Claude API 전용)
- 실행: python3 classifier.py  (미분류 리뷰 일괄 처리)
- OPENAI_API_KEY 환경변수 필요
"""
import json, os, time, re, threading, argparse, unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

REVIEWS_FILE = "data/reviews.json"
_days = 365  # 기본값, __main__에서 오버라이드
_date_from = None  # YYYY-MM-DD, 지정 시 _days 무시
_date_to = None    # YYYY-MM-DD
_reanalyze = False # True 시 이미 분석된(미답변) 리뷰도 재분석
PROGRESS_FILE = "data/classify_progress.json"
BRAND_TONE_FILE = "config/brand_tone.txt"
SETTINGS_FILE = "config/settings.json"


def write_progress(done, total, step="분류 중"):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"done": done, "total": total, "step": step, "running": True}, f)


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"report_criteria": ["욕설", "경쟁사 언급", "광고성", "반복 내용"]}


def load_brand_tone():
    if os.path.exists(BRAND_TONE_FILE):
        with open(BRAND_TONE_FILE, encoding="utf-8") as f:
            return f.read()
    return ""


def api_classify(review: dict, client, report_criteria: list = None,
                 include_reply: bool = False, brand_tone: str = "") -> dict:
    """Claude API 기반 분류 (include_reply=True 시 답변 초안도 한 번에 생성)"""
    if report_criteria is None:
        report_criteria = load_settings().get("report_criteria", ["욕설", "경쟁사 언급", "광고성", "반복 내용"])
    criteria_str = " | ".join(f'"{c}"' for c in report_criteria)

    reply_field = ""
    reply_instruction = ""
    if include_reply:
        tone_section = f"\n\n브랜드 톤 가이드:\n{brand_tone}" if brand_tone else ""
        reply_field = '\n  "reply": "리뷰에 대한 고객 답변 (2~4문장)",'
        reply_instruction = f"{tone_section}\n\n위 리뷰에 브랜드 톤에 맞는 고객 답변도 reply 필드에 작성해줘."

    prompt = f"""다음 리뷰를 분석해서 JSON으로 답해줘. JSON 외 다른 텍스트 없이 JSON만 반환.

리뷰: {review.get('content', '')}
별점: {review.get('rating', '')}점
상품: {review.get('product', '')}
{reply_instruction}
반환 형식:
{{
  "sentiment": "positive" | "negative" | "mixed",
  "topics": ["효능", "성분", "포장", "배송", "가격", "냄새/맛", "복용편의"] 중 해당하는 것들,
  "reportable": true | false,
  "report_reason": {criteria_str} | "",{reply_field}
}}"""

    max_tokens = 768 if include_reply else 150
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=max_tokens,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.choices[0].message.content.strip()
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group())
            break
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e) and attempt == 0:
                time.sleep(2)
            else:
                break
    return {}


# 단일 명사형 표현 뒤에 붙는 한국어 어미/조사 — 선택적 허용
_KOREAN_SUFFIX_RE = r"(?:을|를|이|가|은|는|에|에서|와|과|로|으로|도|만|적|적인|적이|적으로|에도|에는)?"


def _normalize_text(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def _build_sensitive_pattern(expr: str) -> "re.Pattern":
    """등록된 민감 표현을 한국어 변형(어미/조사/공백)까지 흡수하는 정규식으로 변환."""
    expr = _normalize_text(expr)
    parts = expr.split()
    if len(parts) > 1:
        # 어구: 어절 사이 공백 변형(여러 공백) 허용
        body = r"\s+".join(re.escape(p) for p in parts)
        return re.compile(body)
    # 단일 단어: 끝에 어미/조사 변형 허용
    return re.compile(re.escape(expr) + _KOREAN_SUFFIX_RE)


# 정규식 캐시 (settings 핫리로드 대응 위해 키는 표현 문자열)
_sensitive_pattern_cache: dict = {}


def _get_pattern(expr: str) -> "re.Pattern":
    pat = _sensitive_pattern_cache.get(expr)
    if pat is None:
        pat = _build_sensitive_pattern(expr)
        _sensitive_pattern_cache[expr] = pat
    return pat


def _contains_sensitive(text: str, expressions: list) -> list:
    """생성된 답변에서 민감 표현 검출 (어미/조사/공백 변형 흡수). 발견된 원본 표현 목록 반환."""
    if not text or not expressions:
        return []
    text_n = _normalize_text(text)
    found = []
    for expr in expressions:
        if _get_pattern(expr).search(text_n):
            found.append(expr)
    return found


_SAFE_ALTERNATIVES_BLOCK = (
    "\n\n[안전한 대체 표현 예시]\n"
    '- "효과가 있습니다" → "좋은 경험으로 이어지길 바랍니다"\n'
    '- "효과를 보장합니다" → "꾸준히 드시면서 긍정적인 변화를 느끼셨으면 좋겠습니다"\n'
    '- "치료에 도움이 됩니다" → "건강한 일상에 도움이 되길 바랍니다"\n'
    '- "혈압/혈당/콜레스테롤" 등 수치·질환 언급 → 언급 자체를 생략하고 공감 표현으로 대체'
)

MAX_SENSITIVE_RETRIES = 3


def generate_reply(review: dict, brand_tone: str, client, settings: dict = None) -> dict:
    """OpenAI API 기반 답변 생성.

    반환: {"text": str, "sensitive_remaining": list[str], "attempts": int}
      - sensitive_remaining 비어있으면 안전, 채워져 있으면 자동 등록 금지 신호.
    """
    settings = settings or load_settings()
    sensitive = settings.get("sensitive_expressions", [])
    auto_retry = settings.get("auto_retry_sensitive", True)
    model = settings.get("active_model", "gpt-4o-mini")

    system = f"""당신은 건강기능식품 브랜드의 고객 담당자입니다.
아래 브랜드 톤 가이드에 따라 리뷰에 답변하세요.

{brand_tone}

답변만 출력하세요. 따옴표나 설명 없이 답변 텍스트만."""

    if settings.get("spelling_correction", True):
        system += "\n반드시 올바른 한국어 맞춤법과 띄어쓰기를 사용하세요. 오탈자가 없도록 주의하세요."

    if sensitive:
        forbidden_block = "\n\n[절대 사용 금지 표현 — 어떤 변형(어미/조사 포함)으로도 답변에 포함 금지]\n"
        forbidden_block += "\n".join(f"- {e}" for e in sensitive)
        forbidden_block += "\n고객 리뷰에 위 표현이 있더라도 답글에서 반복하거나 단정하지 마세요."
        forbidden_block += _SAFE_ALTERNATIVES_BLOCK
        system += forbidden_block

    customer_type    = review.get("customer_type", "")
    reviewer_history = review.get("reviewer_history", [])
    hints = settings.get("customer_type_hints", {})
    type_label = {"first": "첫 구매", "repeat": "재구매", "loyal": "단골", "gift": "선물 구매자"}.get(customer_type, "")
    type_hint  = hints.get(customer_type, "")
    history_lines = ""
    if reviewer_history:
        recent = sorted(reviewer_history, key=lambda x: x.get("date", ""), reverse=True)[:3]
        history_lines = "\n".join(
            f"  - {h.get('date','')} | 별점 {h.get('rating','')}점 | {(h.get('product','') or '')[:30]} | {(h.get('content','') or '')[:40]}"
            for h in recent
        )
    customer_context = ""
    if type_label:
        customer_context  = f"\n\n[고객 유형: {type_label}]"
        if type_hint:
            customer_context += f"\n{type_hint}"
        if history_lines:
            customer_context += f"\n\n[최근 구매 이력]\n{history_lines}"

    user = f"""리뷰 내용: {review.get('content', '')}
별점: {review.get('rating', '')}점
상품: {review.get('product', '')}{customer_context}

위 리뷰에 적절한 답변을 작성해주세요."""

    def _call(sys_prompt: str) -> str:
        for attempt in range(2):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    max_tokens=512,
                    temperature=0.4,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user},
                    ],
                )
                return resp.choices[0].message.content.strip()
            except Exception as e:
                if "rate_limit" in str(e).lower() or "429" in str(e) and attempt == 0:
                    time.sleep(2)
                else:
                    break
        return ""

    reply_text = _call(system)
    attempts = 1
    found = _contains_sensitive(reply_text, sensitive) if sensitive else []

    while found and auto_retry and attempts <= MAX_SENSITIVE_RETRIES:
        if attempts == 1:
            extra = (
                f"\n\n[재생성 요청 1/{MAX_SENSITIVE_RETRIES}] 이전 답변에 금지 표현"
                f"({', '.join(found)})이 포함되었습니다. 이 표현 및 변형 없이 다시 작성하세요."
            )
        else:
            extra = (
                f"\n\n[재생성 요청 {attempts}/{MAX_SENSITIVE_RETRIES}] 이전 답변에 여전히 금지 표현"
                f"({', '.join(found)})이 포함되어 있습니다. "
                "위 [안전한 대체 표현 예시]를 반드시 따라 다시 작성하세요. "
                "고객 리뷰에 해당 표현이 등장하더라도 답글에서는 절대 그 표현이나 어미 변형을 쓰지 마세요."
            )
        retry_text = _call(system + extra)
        if retry_text:
            reply_text = retry_text
        attempts += 1
        found = _contains_sensitive(reply_text, sensitive) if sensitive else []

    return {
        "text": reply_text,
        "sensitive_remaining": found,
        "attempts": attempts,
    }


def classify_review(review: dict, client, report_criteria: list = None) -> dict:
    """단일 리뷰 분류"""
    return api_classify(review, client, report_criteria)


def process_batch():
    """미분류 리뷰 일괄 처리 (병렬)"""
    if not os.path.exists(REVIEWS_FILE):
        print("reviews.json 없음. 먼저 scraper.py 실행하세요.")
        return

    with open(REVIEWS_FILE, encoding="utf-8") as f:
        reviews = json.load(f)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("오류: OPENAI_API_KEY 환경변수가 없습니다.")
        return

    try:
        from openai import OpenAI
    except ImportError:
        print("오류: openai 패키지 없음. pip install openai")
        return

    client = OpenAI(api_key=api_key)
    print("OpenAI API로 분류 실행 (병렬 25개)")

    brand_tone = load_brand_tone()
    settings = load_settings()
    report_criteria = settings.get("report_criteria", ["욕설", "경쟁사 언급", "광고성", "반복 내용"])
    auto_generate = settings.get("auto_generate_reply", False)

    if _date_from or _date_to:
        df = _date_from or "0000-00-00"
        dt = _date_to or "9999-99-99"
        in_range = lambda r: df <= r.get("date", "") <= dt
    else:
        cutoff = (datetime.now() - timedelta(days=_days)).strftime("%Y-%m-%d") if _days > 0 else None
        in_range = lambda r: cutoff is None or r.get("date", "") >= cutoff

    def needs_classify(r):
        if r.get("replied"):
            return False
        if _reanalyze:
            return True
        return r.get("sentiment") is None

    unclassified = [i for i, r in enumerate(reviews) if needs_classify(r) and in_range(r)]
    total = len(unclassified)
    print(f"미분류 리뷰: {total}건")
    write_progress(0, total, "시작 중")

    lock = threading.Lock()
    done_count = [0]

    def process_one(idx):
        r = reviews[idx]
        result = api_classify(r, client, report_criteria)

        existing_reply = reviews[idx].get("ai_reply", "")
        existing_status = reviews[idx].get("reply_status", "none")

        skip_reportable = settings.get("skip_reportable_reply", False)
        is_reportable = bool(result.get("reportable"))
        new_sensitive_flags = None  # None=손대지 않음, []=초기화, list=세팅
        if auto_generate and not existing_reply and not reviews[idx].get("replied") and not (skip_reportable and is_reportable):
            try:
                gen = generate_reply(r, brand_tone, client, settings=settings)
                existing_reply = gen.get("text", "")
                remaining = gen.get("sensitive_remaining", [])
                if existing_reply:
                    if remaining:
                        existing_status = "needs_review"
                        new_sensitive_flags = remaining
                    else:
                        existing_status = "draft"
                        new_sensitive_flags = []
            except Exception:
                pass

        if existing_reply and existing_status == "none":
            existing_status = "draft"

        with lock:
            reviews[idx].update({
                "sentiment": result.get("sentiment"),
                "topics": result.get("topics", []),
                "reportable": result.get("reportable", False),
                "report_reason": result.get("report_reason", ""),
                "ai_reply": existing_reply,
                "reply_status": existing_status,
            })
            if new_sensitive_flags:
                reviews[idx]["sensitive_flags"] = new_sensitive_flags
            elif new_sensitive_flags == []:
                reviews[idx].pop("sensitive_flags", None)
            done_count[0] += 1
            _done = done_count[0]
        write_progress(_done, total, f"처리 중 ({_done}/{total})")

    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(process_one, i) for i in unclassified]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"오류: {e}")

    with open(REVIEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)

    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"done": total, "total": total, "step": "완료", "running": False}, f)

    print(f"완료: {total}건 처리됨")

    sentiments = [r.get("sentiment") for r in reviews if r.get("sentiment")]
    print(f"\n감성 분포:")
    for s in ["positive", "negative", "mixed"]:
        cnt = sentiments.count(s)
        label = {"positive": "긍정", "negative": "부정", "mixed": "혼합"}[s]
        print(f"  {label}: {cnt}건")
    reportable = sum(1 for r in reviews if r.get("reportable"))
    print(f"신고 가능: {reportable}건")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365, help="최근 N일 리뷰만 분류 (0=전체)")
    parser.add_argument("--date-from", type=str, default=None, help="시작일 YYYY-MM-DD (지정 시 --days 무시)")
    parser.add_argument("--date-to", type=str, default=None, help="종료일 YYYY-MM-DD")
    parser.add_argument("--reanalyze", action="store_true", help="이미 분석된 리뷰(미답변)도 재분석")
    args = parser.parse_args()
    _days = args.days
    _date_from = args.date_from
    _date_to = args.date_to
    _reanalyze = args.reanalyze
    process_batch()
