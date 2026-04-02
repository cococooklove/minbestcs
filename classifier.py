"""
리뷰 분류 및 AI 답변 생성
- ANTHROPIC_API_KEY 환경변수 없으면 규칙 기반 분류로 대체
- 실행: python3 classifier.py  (미분류 리뷰 일괄 처리)
"""
import json, os, time, re

REVIEWS_FILE = "data/reviews.json"
BRAND_TONE_FILE = "config/brand_tone.txt"

# 규칙 기반 분류 키워드
POSITIVE_KEYWORDS = ["좋아", "좋았", "만족", "훌륭", "최고", "맛있", "효과", "강추", "추천", "재구매", "기대이상", "대박", "완벽", "빠른", "친절"]
NEGATIVE_KEYWORDS = ["별로", "실망", "불만", "최악", "나쁘", "아쉽", "환불", "교환", "파손", "늦", "불친절", "비싸", "효과없", "소용없"]
TOPIC_KEYWORDS = {
    "효능": ["효과", "효능", "도움", "변화", "개선", "좋아졌"],
    "성분": ["성분", "원료", "첨가물", "함량", "영양"],
    "포장": ["포장", "박스", "케이스", "용기", "밀봉", "파손"],
    "배송": ["배송", "배달", "도착", "빠른", "늦", "택배"],
    "가격": ["가격", "비싸", "저렴", "가성비", "돈"],
    "냄새/맛": ["냄새", "맛", "향", "쓴맛", "냄새나"],
    "복용편의": ["먹기", "복용", "알약", "캡슐", "크기"],
}
REPORT_KEYWORDS = {
    "욕설": ["씨", "개같", "ㅅㅂ", "ㅂㅅ", "미친", "병신"],
    "경쟁사 언급": ["타사", "다른 브랜드", "경쟁"],
    "광고성": ["협찬", "무료로 받", "제공받"],
}


def load_brand_tone():
    if os.path.exists(BRAND_TONE_FILE):
        with open(BRAND_TONE_FILE, encoding="utf-8") as f:
            return f.read()
    return ""


def rule_based_classify(review: dict) -> dict:
    """API 키 없을 때 키워드 기반 분류"""
    content = review.get("content", "").lower()

    # 감성 분류
    pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in content)
    neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in content)
    rating = float(review.get("rating", 3) or 3)

    if rating >= 4 and neg == 0:
        sentiment = "positive"
    elif rating <= 2 or neg > pos:
        sentiment = "negative"
    elif pos > 0 and neg > 0:
        sentiment = "mixed"
    elif rating >= 4:
        sentiment = "positive"
    else:
        sentiment = "mixed"

    # 주제 태깅
    topics = [topic for topic, kws in TOPIC_KEYWORDS.items() if any(kw in content for kw in kws)]

    # 신고 가능 여부
    reportable = False
    report_reason = ""
    for reason, kws in REPORT_KEYWORDS.items():
        if any(kw in content for kw in kws):
            reportable = True
            report_reason = reason
            break

    return {
        "sentiment": sentiment,
        "topics": topics,
        "reportable": reportable,
        "report_reason": report_reason,
    }


def api_classify(review: dict, client) -> dict:
    """Claude API 기반 분류"""
    prompt = f"""다음 리뷰를 분석해서 JSON으로 답해줘. JSON 외 다른 텍스트 없이 JSON만 반환.

리뷰: {review.get('content', '')}
별점: {review.get('rating', '')}점
상품: {review.get('product', '')}

반환 형식:
{{
  "sentiment": "positive" | "negative" | "mixed",
  "topics": ["효능", "성분", "포장", "배송", "가격", "냄새/맛", "복용편의"] 중 해당하는 것들,
  "reportable": true | false,
  "report_reason": "욕설" | "경쟁사 언급" | "광고성" | "반복 내용" | ""
}}"""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    # JSON 추출
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return rule_based_classify(review)


def generate_reply(review: dict, brand_tone: str, client) -> str:
    """Claude API 기반 답변 생성"""
    system = f"""당신은 건강기능식품 브랜드의 고객 담당자입니다.
아래 브랜드 톤 가이드에 따라 리뷰에 답변하세요.

{brand_tone}

답변만 출력하세요. 따옴표나 설명 없이 답변 텍스트만."""

    user = f"""리뷰 내용: {review.get('content', '')}
별점: {review.get('rating', '')}점
상품: {review.get('product', '')}

위 리뷰에 적절한 답변을 작성해주세요."""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip()


def classify_review(review: dict, client=None) -> dict:
    """단일 리뷰 분류 (API 없으면 규칙 기반)"""
    if client:
        try:
            return api_classify(review, client)
        except Exception as e:
            print(f"  API 오류, 규칙 기반으로 대체: {e}")
    return rule_based_classify(review)


def process_batch():
    """미분류 리뷰 일괄 처리"""
    if not os.path.exists(REVIEWS_FILE):
        print("reviews.json 없음. 먼저 scraper.py 실행하세요.")
        return

    with open(REVIEWS_FILE, encoding="utf-8") as f:
        reviews = json.load(f)

    # Claude API 초기화 (키 있으면)
    client = None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            print("Claude API 모드로 실행")
        except ImportError:
            print("anthropic 패키지 없음. pip install anthropic")
    else:
        print("ANTHROPIC_API_KEY 없음 → 규칙 기반 분류 실행")

    brand_tone = load_brand_tone()
    unclassified = [i for i, r in enumerate(reviews) if r.get("sentiment") is None]
    print(f"미분류 리뷰: {len(unclassified)}건")

    updated = 0
    for i in unclassified:
        r = reviews[i]
        result = classify_review(r, client)
        reviews[i].update({
            "sentiment": result.get("sentiment"),
            "topics": result.get("topics", []),
            "reportable": result.get("reportable", False),
            "report_reason": result.get("report_reason", ""),
            "ai_reply": reviews[i].get("ai_reply", ""),
            "reply_status": reviews[i].get("reply_status", "none"),
        })
        updated += 1
        if updated % 10 == 0:
            print(f"  {updated}/{len(unclassified)} 처리 중...")
        if client:
            time.sleep(0.3)

    with open(REVIEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)

    print(f"완료: {updated}건 분류됨")

    # 통계 출력
    sentiments = [r.get("sentiment") for r in reviews if r.get("sentiment")]
    print(f"\n감성 분포:")
    for s in ["positive", "negative", "mixed"]:
        cnt = sentiments.count(s)
        label = {"positive": "긍정", "negative": "부정", "mixed": "혼합"}[s]
        print(f"  {label}: {cnt}건")
    reportable = sum(1 for r in reviews if r.get("reportable"))
    print(f"신고 가능: {reportable}건")


if __name__ == "__main__":
    process_batch()
