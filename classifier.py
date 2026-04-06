"""
리뷰 분류 및 AI 답변 생성 (Claude API 전용)
- 실행: python3 classifier.py  (미분류 리뷰 일괄 처리)
- ANTHROPIC_API_KEY 환경변수 필요
"""
import json, os, time, re, threading, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

REVIEWS_FILE = "data/reviews.json"
_days = 365  # 기본값, __main__에서 오버라이드
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

    max_tokens = 768 if include_reply else 256
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
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


def generate_reply(review: dict, brand_tone: str, client) -> str:
    """Claude API 기반 답변 생성 (단독 호출용 fallback)"""
    system = f"""당신은 건강기능식품 브랜드의 고객 담당자입니다.
아래 브랜드 톤 가이드에 따라 리뷰에 답변하세요.

{brand_tone}

답변만 출력하세요. 따옴표나 설명 없이 답변 텍스트만."""

    customer_type    = review.get("customer_type", "")
    reviewer_history = review.get("reviewer_history", [])
    hints = load_settings().get("customer_type_hints", {})
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

    for attempt in range(2):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e) and attempt == 0:
                time.sleep(2)
            else:
                break
    return ""


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

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("오류: ANTHROPIC_API_KEY 환경변수가 없습니다.")
        return

    try:
        import anthropic
    except ImportError:
        print("오류: anthropic 패키지 없음. pip install anthropic")
        return

    client = anthropic.Anthropic(api_key=api_key)
    print("Claude API로 분류 실행 (병렬 5개)")

    brand_tone = load_brand_tone()
    settings = load_settings()
    report_criteria = settings.get("report_criteria", ["욕설", "경쟁사 언급", "광고성", "반복 내용"])
    auto_generate = settings.get("auto_generate_reply", False)

    cutoff = (datetime.now() - timedelta(days=_days)).strftime("%Y-%m-%d") if _days > 0 else None
    unclassified = [i for i, r in enumerate(reviews)
                    if r.get("sentiment") is None and not r.get("replied")
                    and (cutoff is None or r.get("date", "") >= cutoff)]
    total = len(unclassified)
    print(f"미분류 리뷰: {total}건")
    write_progress(0, total, "시작 중")

    lock = threading.Lock()
    done_count = [0]

    def process_one(idx):
        r = reviews[idx]
        result = api_classify(r, client, report_criteria,
                              include_reply=auto_generate, brand_tone=brand_tone)

        existing_reply = result.get("reply", "") or reviews[idx].get("ai_reply", "")
        existing_status = reviews[idx].get("reply_status", "none")

        # include_reply 실패 시 fallback
        if auto_generate and not existing_reply and not reviews[idx].get("replied"):
            try:
                existing_reply = generate_reply(r, brand_tone, client)
                existing_status = "draft"
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
            done_count[0] += 1
            write_progress(done_count[0], total, f"처리 중 ({done_count[0]}/{total})")

    with ThreadPoolExecutor(max_workers=5) as executor:
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
    args = parser.parse_args()
    _days = args.days
    process_batch()
