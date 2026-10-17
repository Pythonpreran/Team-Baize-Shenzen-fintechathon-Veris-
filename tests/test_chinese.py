"""T28: Chinese (zh-CN) language plumbing and the China pack.

What this pins:
  * a Han-script document gets character bigrams, and English keeps its exact contract;
  * Chinese numerals, renminbi amounts (元, 万元, 亿, 大写), year-first dates and 第…条/款
    clause numbers are read exactly, and anything ambiguous yields no value;
  * every guard applies the Chinese outcome and advice lexicons, and the grounding gate
    rejects an unsupported figure even when it is written in Chinese characters;
  * a Chinese question about payment or surrender goes to authorised review;
  * the deepseek and qwen profiles (and only they) get the Chinese system prompt, over a
    mocked transport — no live model call;
  * mainland resident ID numbers, mobiles, labelled names and addresses are redacted
    reversibly; OCR reports chi_sim only when the engine lists it;
  * the China pack shows an owner only what an HTTPS primary source was checked to say.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from proven import ai, jurisdiction, llm, ocr, privacy, qa, redact, retrieval
from proven.extract import find_date, find_money_with_currency
from proven.lang import (
    ChineseBigramTokenizer,
    EnglishTokenizer,
    chinese_numeral_value,
    clause_number,
    date_parser_for,
    money_parser_for,
    tokenizer_for_document,
    tokenizer_for_query,
)


class _Response:
    def __init__(self, body: dict):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._body).encode("utf-8")


@pytest.fixture(autouse=True)
def _clean_llm(monkeypatch):
    llm.clear_runtime_state()
    for name in ("VERIS_LLM_PROFILE", "VERIS_LLM_BASE_URL", "VERIS_LLM_MODEL",
                 "VERIS_LLM_MODEL_VERSION", "VERIS_LLM_API_KEY", "OPENAI_API_KEY",
                 "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield
    llm.clear_runtime_state()


# ── tokenizer and retrieval ─────────────────────────────────────────────────

def test_han_text_gets_character_bigrams_and_english_keeps_its_contract():
    tokenizer = tokenizer_for_document("交强险条款第五条")
    assert isinstance(tokenizer, ChineseBigramTokenizer)
    assert tokenizer.tokenize("交强险，条款") == ("交强", "强险", "条款")
    assert tokenizer.tokenize("赔偿限额 180000 元") == ("赔偿", "偿限", "限额", "180000", "元")
    assert isinstance(tokenizer_for_query("When does it end?"), EnglishTokenizer)
    assert isinstance(tokenizer_for_document("Policy wording"), EnglishTokenizer)


def test_bigram_retrieval_ranks_the_chinese_clause_that_answers_the_question():
    pages = ["第一条 被保险人应当在保险事故发生后及时通知保险人。",
             "第二条 住院医疗费用的等待期为三十日，续保不受等待期限制。"]
    chunks = []
    for page_no, text in enumerate(pages, start=1):
        chunks.extend(retrieval.chunk_page(text, page_no=page_no, document_id="cn-doc",
                                           document_kind="BASE_WORDING", filename="cn.txt",
                                           start_id=len(chunks)))
    hits = retrieval.Retriever(chunks).search("等待期多长？", top_k=1)
    assert hits and hits[0].chunk.page_no == 2


# ── numerals, money, dates, clause numbers ──────────────────────────────────

@pytest.mark.parametrize(("text", "value"), [
    ("十一", 11), ("一千零五", 1005), ("伍拾万", 500000), ("三亿五千万", 350000000),
    ("二〇二六", 2026), ("两万", 20000), ("十", 10),
])
def test_chinese_numerals_are_read_exactly(text, value):
    assert chinese_numeral_value(text) == value


@pytest.mark.parametrize("text", ["万", "亿", "保险", ""])
def test_a_unit_heading_or_a_word_is_not_a_number(text):
    assert chinese_numeral_value(text) is None


def test_renminbi_amounts_in_every_written_form():
    cny = money_parser_for("CNY")
    assert cny.parse_money("人民币伍拾万元整") == Decimal("500000")
    assert cny.parse_money("50万元") == Decimal("500000")
    assert cny.parse_money("1.5亿元") == Decimal("150000000")
    assert cny.parse_money("￥18,000") == Decimal("18000")
    assert cny.parse_money("RMB 2,000") == Decimal("2000")
    assert cny.parse_money("壹佰贰拾叁元肆角伍分") == Decimal("123.45")
    # No marker and no 元: a bare figure, not money evidence, unless the caller allows it.
    assert cny.parse_money("赔偿限额 2000") is None
    assert cny.parse_money("赔偿限额 2000", allow_bare=True) == Decimal("2000")
    assert isinstance(cny.parse_money("50万元"), Decimal)


def test_a_labelled_chinese_amount_names_cny_and_its_restatement_is_one_value():
    hit = find_money_with_currency(["保险金额：人民币50万元（伍拾万元整）"], ("保险金额",),
                                   currency=None)
    assert hit is not None
    assert (hit[0], hit[3]) == (Decimal("500000"), "CNY")
    assert hit[1] == "保险金额：人民币50万元"
    # Two different amounts in one field are ambiguous, so nothing is extracted.
    assert find_money_with_currency(["保险金额：50万元（陆拾万元整）"], ("保险金额",),
                                    currency=None) is None
    # An explicit foreign marker in a Chinese document keeps its own currency.
    hit = find_money_with_currency(["免赔额：HK$500"], ("免赔额",), currency=None)
    assert hit is not None and hit[3] == "HKD"


def test_chinese_dates_are_year_first_and_never_guessed():
    parser = date_parser_for("CNY")
    assert parser.parse_date("2026年10月11日零时起") == date(2026, 10, 11)
    assert parser.parse_date("二〇二六年十月十一日") == date(2026, 10, 11)
    assert parser.parse_date("2026-10-11") == date(2026, 10, 11)
    assert parser.parse_date("2026/3/1") == date(2026, 3, 1)
    assert parser.parse_date("11/10/2026") is None
    assert parser.parse_date("2026年2月30日") is None
    hit = find_date(["保险期间：自2026年3月1日零时起至2027年2月28日二十四时止"], ("保险期间",),
                    currency="CNY")
    assert hit is not None and hit[0] == date(2026, 3, 1)


def test_chinese_clause_numbers():
    assert clause_number("依照第二十三条第二款的规定") == "23(2)"
    assert clause_number("第5条") == "5"
    assert clause_number("第一百零一条第一款第三项") == "101(1)(3)"
    assert clause_number("本条款") is None


# ── guards ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "保险公司一定会赔付这笔费用。", "这次住院不会赔。", "这项手术属于保险责任。",
    "您违反了告知义务。", "保单已经失效。", "可以全额报销。", "您没有保障。",
])
def test_chinese_outcome_language_is_refused_by_every_guard(text):
    assert ai._asserts_outcome(text)
    ok, why = llm.guard_user_visible(text, text, citations=["c"])
    assert ok is False and "outcome" in why


@pytest.mark.parametrize("text", [
    "建议您退保。", "推荐您购买这款产品。", "可以考虑转保。", "建议您取消保单。",
])
def test_chinese_product_advice_is_refused(text):
    assert ai._asserts_advice(text)
    ok, why = llm.guard_user_visible(text, text, citations=["c"])
    assert ok is False


@pytest.mark.parametrize("text", [
    "条款列明了理赔所需的文件。", "请提供出院小结和费用清单。", "建议您提供出院小结。",
])
def test_neutral_chinese_descriptions_are_not_mistaken_for_verdicts_or_advice(text):
    assert not ai._asserts_outcome(text)
    assert not ai._asserts_advice(text)


def test_an_unsupported_figure_written_in_chinese_characters_is_rejected():
    ok, why = llm.guard_user_visible("等待期为三十日。", "The waiting period is 90 days.",
                                     citations=["c"])
    assert ok is False and "unsupported number" in why
    ok, why = llm.guard_user_visible("等待期为三十日。", "等待期为30日。", citations=["c"])
    assert ok is True, why
    ok, why = llm.guard_user_visible("等待期为30日。", "等待期为三十日。", citations=["c"])
    assert ok is True, why
    # 一定 ("certainly") is not the number one.
    ok, why = llm.guard_user_visible("请一定保留出院小结。", "保留出院小结。", citations=["c"])
    assert ok is True, why


def test_a_chinese_statement_about_a_policy_needs_a_citation():
    ok, why = llm.guard_user_visible("保单第五条列明了等待期。", "保单第五条列明了等待期。")
    assert ok is False and "citation" in why


@pytest.mark.parametrize("question", ["住院费用会赔吗？", "我应该退保吗？", "能报销吗？",
                                      "这款保险值得买吗？"])
def test_chinese_outcome_or_advice_questions_go_to_authorised_review(question):
    assert qa._intent(question) == "authorised_review"


# ── Chinese prompts on the China-reachable profiles (mocked transport) ──────

@pytest.mark.parametrize("profile", ["deepseek", "qwen"])
def test_china_reachable_profiles_get_the_chinese_prompt_for_a_chinese_question(
        monkeypatch, profile):
    monkeypatch.setenv("VERIS_LLM_PROFILE", profile)
    monkeypatch.setenv("VERIS_LLM_API_KEY", "test-provider-token")
    assert llm.prompt_language("等待期什么时候结束？") == "zh"
    assert llm.prompt_language("When does the waiting period end?") == "en"
    assert llm.prompt_language("When does it end?", locale="zh-CN") == "zh"
    captured = []

    def fake_urlopen(request, **_kwargs):
        captured.append(json.loads(request.data.decode("utf-8")))
        return _Response({"model": f"{profile}-test", "choices": [
            {"message": {"content": "CITED"}}]})

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    answer = qa.Answer(text="The waiting period ends on 2027-04-01.",
                       output_state="DOCUMENTED", evidence_class="PROVEN",
                       citations=({"quote": "The waiting period ends on 2027-04-01."},))
    out = qa.maybe_model_rephrase(answer, "等待期什么时候结束？", model_allowed=True,
                                  tenant_id="tenant-zh")
    assert len(captured) == 1
    assert captured[0]["messages"][0]["content"] == qa._MODEL_SYSTEM_ZH
    assert out.mode == "LLM_ASSISTED"


def test_other_profiles_keep_the_english_prompt_for_a_chinese_question(monkeypatch):
    monkeypatch.setenv("VERIS_LLM_PROFILE", "openai")
    monkeypatch.setenv("VERIS_LLM_API_KEY", "test-provider-token")
    assert llm.prompt_language("等待期什么时候结束？", locale="zh-CN") == "en"
    assert llm.localised_system("EN", "ZH", text="等待期什么时候结束？") == "EN"
    monkeypatch.setenv("VERIS_LLM_PROFILE", "none")
    assert llm.prompt_language("等待期什么时候结束？") == "en"


# ── redaction ───────────────────────────────────────────────────────────────

def _cn_id(body: str) -> str:
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    total = sum(int(d) * w for d, w in zip(body, weights))
    return body + "10X98765432"[total % 11]


def test_mainland_identity_numbers_mobiles_names_and_addresses_are_redacted():
    rid = _cn_id("44030519900101123")
    assert redact.valid_cn_resident_id(rid)
    wrong = rid[:-1] + ("1" if rid[-1] != "1" else "2")
    assert not redact.valid_cn_resident_id(wrong)
    assert not redact.valid_cn_resident_id(_cn_id("44030519901301123"))  # month 13
    source = (f"被保险人：张小明，身份证号{rid}，手机13812345678，"
              "地址：广东省深圳市示例路1号。")
    vault = redact.Redactor()
    out = vault.redact_text(source)
    for raw in ("张小明", rid, "13812345678", "广东省深圳市示例路1号"):
        assert raw not in out, raw
    assert vault.token_summary().get("CN_RESIDENT_ID") == 1
    assert vault.rehydrate(out) == source


# ── OCR language detection ──────────────────────────────────────────────────

class _FakeOCR:
    name = "tesseract"

    def __init__(self, langs):
        self._langs = tuple(langs)

    def available(self):
        return True

    def version(self):
        return "tesseract 5 (fake)"

    def ocr_image(self, data, page_no=1):  # pragma: no cover - never called here
        raise AssertionError("not used")

    def languages(self):
        return self._langs


def test_ocr_reports_chinese_only_when_the_engine_lists_chi_sim(monkeypatch):
    monkeypatch.setenv("PROVEN_OCR", "auto")
    try:
        ocr.set_provider(_FakeOCR(["eng", "chi_sim"]))
        status = ocr.status()
        assert status["languages"] == ["eng", "chi_sim"]
        assert status["chinese_simplified"] is True
        ocr.set_provider(_FakeOCR(["eng"]))
        assert ocr.status()["chinese_simplified"] is False
    finally:
        ocr.set_provider(None)
    assert ocr.parse_language_list(
        'List of available languages in "/usr/share/tessdata/" (2):\nchi_sim\neng\n') == \
        ("chi_sim", "eng")


# ── the China pack ──────────────────────────────────────────────────────────

def test_the_china_pack_shows_an_owner_only_the_https_quoted_hotline():
    cn = jurisdiction.pack("CN")
    assert (cn.currency.code, cn.timezone) == ("CNY", "Asia/Shanghai")
    assert jurisdiction.format_money(Decimal("500000"), "CNY") == "¥500,000"
    owner_facing = {e.id for e in cn.entries() if e.owner_facing}
    assert owner_facing == {"CN-CL-NFRA-HOTLINE"}
    hotline = cn.entry("CN-CL-NFRA-HOTLINE")
    assert hotline.verification.source_url.startswith("https://")
    assert hotline.values["hotline"] in hotline.verification.quote
    for entry_id in ("CN-REG-NFRA", "CN-STAT-INSURANCE-LAW", "CN-STAT-PIPL-SENSITIVE",
                     "CN-STAT-CTALI", "CN-COMP-MOTOR-CTALI", "CN-TL-CLAIM-DETERMINATION",
                     "CN-DP-PIPL"):
        entry = cn.entry(entry_id)
        assert entry.verification.status == "CANDIDATE" and not entry.owner_facing
        # No figure from an unverified source sits in a statement or a value.
        assert "30" not in entry.statement and "two years" not in entry.statement
        assert "days" not in entry.values and "years" not in entry.values
    assert privacy.notices(["CN"])[0]["state"] == "INFORMATION_MISSING"


def test_a_chinese_wording_is_detected_as_china_and_read_under_its_pack():
    pages = ["本保险合同适用中华人民共和国法律。保险金额：人民币50万元。"
             "保险人应当依照中华人民共和国保险法履行义务。"]
    applied = jurisdiction.assess([("cn.txt", pages)])
    assert applied.detection.code == "CN"
    assert applied.pack.code == "CN" and applied.pack_available
    assert "Singapore" not in (applied.notice or "")


def test_a_hong_kong_chinese_heading_is_not_read_as_mainland_law():
    pages = ["本保单受中华人民共和国香港特别行政区法律管辖。"]
    applied = jurisdiction.assess([("hk.txt", pages)])
    assert applied.detection.code != "CN"
