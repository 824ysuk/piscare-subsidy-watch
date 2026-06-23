"""collect.py のタイトルベース deterministic filter の単体テスト。

実観測の noise / signal を fixture 化し、各 Layer が期待通り deny / allow する
ことを確認する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from collect import (  # noqa: E402
    APPLICABLE_FUKUOKA_CITIES,
    FUKUOKA_CITIES,
    NON_TARGET_PREFECTURES,
    classify_admin_prefix,
    classify_known_municipality_prefix,
    detect_known_municipality_at_start,
    detect_non_target_prefecture_in_prefix,
    is_industry_excluded,
    is_jgrants_target,
    is_jnet21_target,
    is_title_target,
    is_type_excluded,
)


def _jnet21_item(title: str) -> dict[str, str]:
    return {"url": "https://example.com/", "title": title, "source": "j-net21"}


def _jgrants_item(title: str, area: str = "福岡県") -> dict[str, str]:
    return {
        "url": "https://example.com/",
        "title": title,
        "source": "jgrants",
        "area": area,
    }


class TestConstantsInvariants(unittest.TestCase):
    """定数間の不変条件を確認する。"""

    def test_applicable_cities_subset_of_fukuoka_cities(self) -> None:
        self.assertTrue(APPLICABLE_FUKUOKA_CITIES <= FUKUOKA_CITIES)

    def test_fukuoka_cities_count(self) -> None:
        # 福岡県は 29 市
        self.assertEqual(len(FUKUOKA_CITIES), 29)

    def test_non_target_prefectures_count(self) -> None:
        # 47 都道府県 - 福岡県 = 46
        self.assertEqual(len(NON_TARGET_PREFECTURES), 46)

    def test_fukuoka_not_in_non_target(self) -> None:
        self.assertNotIn("福岡県", NON_TARGET_PREFECTURES)


class TestClassifyAdminPrefix(unittest.TestCase):
    """Layer 1: 【...】 prefix の解析。"""

    def test_no_prefix_returns_full_title_as_body(self) -> None:
        allow, reason, body = classify_admin_prefix("令和8年度業務改善助成金")
        self.assertTrue(allow)
        self.assertEqual(reason, "no-prefix")
        self.assertEqual(body, "令和8年度業務改善助成金")

    def test_fukuoka_prefecture_allowed_with_stripped_body(self) -> None:
        allow, reason, body = classify_admin_prefix("【福岡県】補助金・助成金：〇〇")
        self.assertTrue(allow)
        self.assertEqual(reason, "prefecture")
        self.assertEqual(body, "補助金・助成金：〇〇")

    def test_kurume_city_allowed(self) -> None:
        allow, reason, _ = classify_admin_prefix("【久留米市】〇〇補助金")
        self.assertTrue(allow)
        self.assertEqual(reason, "applicable-fukuoka-city")

    def test_kurume_composite_allowed(self) -> None:
        allow, reason, _ = classify_admin_prefix("【福岡県久留米市】〇〇補助金")
        self.assertTrue(allow)
        self.assertEqual(reason, "applicable-fukuoka-city")

    def test_other_prefectures_denied(self) -> None:
        for prefix in ("【熊本県】", "【千葉県】", "【東京都】", "【大阪府】", "【北海道】"):
            allow, reason, _ = classify_admin_prefix(f"{prefix}〇〇補助金")
            self.assertFalse(allow, msg=f"prefix={prefix} should be denied")
            self.assertTrue(reason.startswith("other-prefecture:"))

    def test_other_fukuoka_cities_denied(self) -> None:
        for prefix in ("【福岡市】", "【宗像市】", "【北九州市】", "【糸島市】", "【古賀市】"):
            allow, reason, _ = classify_admin_prefix(f"{prefix}〇〇補助金")
            self.assertFalse(allow, msg=f"prefix={prefix} should be denied")
            self.assertTrue(reason.startswith("other-fukuoka-city:"))

    def test_fukuoka_composite_other_city_denied(self) -> None:
        allow, reason, _ = classify_admin_prefix("【福岡県宗像市】〇〇補助金")
        self.assertFalse(allow)
        self.assertEqual(reason, "other-fukuoka-city:宗像市")

    def test_unknown_municipality_outside_fukuoka_denied(self) -> None:
        # 古河市 (茨城県) は FUKUOKA_CITIES にないが 「市」 終わりなので unknown-municipality として deny
        allow, reason, _ = classify_admin_prefix("【古河市】〇〇補助金")
        self.assertFalse(allow)
        self.assertEqual(reason, "unknown-municipality:古河市")

    def test_unknown_prefix_allowed_with_body(self) -> None:
        # 【全国】 等の県/市町村でない prefix は allow に倒し body を返す
        allow, reason, body = classify_admin_prefix("【全国】古賀市〇〇補助金")
        self.assertTrue(allow)
        self.assertEqual(reason, "unknown-prefix")
        self.assertEqual(body, "古賀市〇〇補助金")


class TestClassifyKnownMunicipalityPrefix(unittest.TestCase):
    """Layer 1b: 既知福岡県市町村名の whitelist 判定。"""

    def test_no_municipality_allowed(self) -> None:
        allow, reason = classify_known_municipality_prefix("令和8年度国制度補助金")
        self.assertTrue(allow)
        self.assertEqual(reason, "no-known-municipality")

    def test_kurume_allowed(self) -> None:
        allow, _ = classify_known_municipality_prefix("久留米市WLB 補助金")
        self.assertTrue(allow)

    def test_fukuoka_kurume_composite_allowed(self) -> None:
        allow, _ = classify_known_municipality_prefix("福岡県久留米市〇〇補助金")
        self.assertTrue(allow)

    def test_koga_denied(self) -> None:
        allow, reason = classify_known_municipality_prefix("古賀市温室効果ガス排出量可視化システム導入費補助金")
        self.assertFalse(allow)
        self.assertEqual(reason, "non-applicable-fukuoka-city:古賀市")

    def test_fukuoka_city_denied(self) -> None:
        allow, _ = classify_known_municipality_prefix("福岡市中小企業奨学金返還支援事業")
        self.assertFalse(allow)

    def test_shichouson_renkei_not_matched(self) -> None:
        # 「市町村連携〇〇」は既知福岡県市町村でないため no-opinion (allow)。
        # 旧 regex 実装ではここで「市町村」を抽出して false positive で deny されていた。
        allow, reason = classify_known_municipality_prefix("市町村連携介護人材確保事業補助金")
        self.assertTrue(allow)
        self.assertEqual(reason, "no-known-municipality")

    def test_central_market_not_matched(self) -> None:
        # 「中央卸売市場〇〇」は既知福岡県市町村でないため no-opinion (allow)。
        # 旧 regex 実装では「中央卸売市」を抽出して false positive で deny されていた。
        allow, _ = classify_known_municipality_prefix("中央卸売市場機能強化補助金")
        self.assertTrue(allow)

    def test_shimin_not_matched(self) -> None:
        # 「市民〇〇」は既知福岡県市町村でないため allow。
        allow, _ = classify_known_municipality_prefix("市民健康保険対策補助金")
        self.assertTrue(allow)


class TestDetectKnownMunicipalityAtStart(unittest.TestCase):
    """既知福岡県市町村の判定 helper。29 市すべてを網羅。"""

    def test_all_fukuoka_cities_detected(self) -> None:
        for city in FUKUOKA_CITIES:
            with self.subTest(city=city):
                self.assertEqual(
                    detect_known_municipality_at_start(f"{city}〇〇補助金"),
                    city,
                    msg=f"city={city} should be detected",
                )

    def test_all_fukuoka_cities_detected_with_prefecture_prefix(self) -> None:
        for city in FUKUOKA_CITIES:
            with self.subTest(city=city):
                self.assertEqual(
                    detect_known_municipality_at_start(f"福岡県{city}〇〇補助金"),
                    city,
                )

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(detect_known_municipality_at_start("古河市〇〇"))
        self.assertIsNone(detect_known_municipality_at_start("市町村連携〇〇"))
        self.assertIsNone(detect_known_municipality_at_start("中央卸売市場〇〇"))


class TestDetectNonTargetPrefectureInPrefix(unittest.TestCase):
    """Layer 1c: title 先頭の他県マーカー検出。"""

    def test_other_prefecture_in_prefix_detected(self) -> None:
        self.assertEqual(
            detect_non_target_prefecture_in_prefix("兵庫県の中小事業者向け補助金"),
            "兵庫県",
        )

    def test_fukuoka_in_prefix_not_detected(self) -> None:
        # 福岡県は NON_TARGET_PREFECTURES に含まれないため None
        self.assertIsNone(detect_non_target_prefecture_in_prefix("福岡県の介護補助金"))

    def test_other_prefecture_only_in_body_not_detected(self) -> None:
        # 先頭 PREFIX_SCAN_LENGTH (12) 文字以降にあれば検出しない
        # （多地域比較 title での false positive 防止）
        self.assertIsNone(
            detect_non_target_prefecture_in_prefix("令和8年度介護人材確保事業（兵庫県を参考）")
        )

    def test_partial_prefecture_name_not_detected(self) -> None:
        # 「東京」だけでは "東京都" の完全名と一致しないため検出しない
        self.assertIsNone(detect_non_target_prefecture_in_prefix("東京・福岡比較セミナー"))


class TestIsTypeExcluded(unittest.TestCase):
    """Layer 2: type prefix の除外判定。body を引数に取る。"""

    def test_seminar_excluded(self) -> None:
        self.assertTrue(is_type_excluded("セミナー・イベント：〇〇"))

    def test_expert_call_excluded(self) -> None:
        self.assertTrue(is_type_excluded("専門家向け公募：よろず支援拠点"))

    def test_event_exhibitor_excluded(self) -> None:
        self.assertTrue(is_type_excluded("イベント出展者募集：BEYOND SDGs"))

    def test_recruitment_excluded(self) -> None:
        # 「募集：」は J-Net21 では SBIR / RFI 等の非補助金情報で支配的なため deny。
        # 補助金本体の「募集：〇〇補助金」が観測されたら見直す。
        self.assertTrue(is_type_excluded("募集：SBIR制度に係るRFI"))

    def test_subsidy_keyword_kept(self) -> None:
        self.assertFalse(is_type_excluded("補助金・助成金：令和8年度〇〇"))

    def test_no_colon_kept(self) -> None:
        self.assertFalse(is_type_excluded("令和8年度業務改善助成金"))


class TestIsIndustryExcluded(unittest.TestCase):
    """Layer 3: 業種固有 keyword の除外判定。"""

    def test_agriculture_excluded(self) -> None:
        self.assertTrue(is_industry_excluded("【千葉県】肥料価格高騰緊急支援事業（農業者向け）"))

    def test_fishery_excluded(self) -> None:
        self.assertTrue(is_industry_excluded("【網走市】水産業パワーアップ事業補助金"))

    def test_forestry_excluded(self) -> None:
        self.assertTrue(is_industry_excluded("令和8年度「A-wood」林業需要拡大事業補助金"))

    def test_nursing_kept(self) -> None:
        self.assertFalse(is_industry_excluded("【福岡県】訪問看護事業所処遇改善補助金"))


class TestIsJnet21TargetSlackNoise(unittest.TestCase):
    """Slack 実観測の noise (J-Net21 経由) が deny されることを確認する。"""

    NOISE_CASES = [
        # type prefix 系
        "セミナー・イベント：「福岡県海外駐在員とのネットワーキングセミナー in 北九州 2026」",
        "セミナー・イベント：「福岡県海外駐在員とのネットワーキングセミナー in 福岡 2026」",
        "専門家向け公募：「福岡県よろず支援拠点生産性向上支援センター 「生産性向上支援サポーター」公募要領」",
        "【福岡県】セミナー・イベント：「福岡県 脱炭素経営 はじめの一歩。応援セミナー」",
        # 福岡県内他市町村 (【】 prefix 系)
        "【福岡県宗像市】令和8年度 宗像市がんばる中小企業者応援補助金",
        "【福岡県宗像市】令和8年度 宗像市創業応援補助金（“宗業”者応援補助金）",
        "【福岡県宗像市】令和8年度 食のまち宗像推進補助金",
        "【福岡市】イベント出展者募集：「BEYOND SDGs エコプロ」共同出展企業の募集について",
        "【福岡市】補助金・助成金 ：「中小企業奨学金返還支援事業（補助金）」",
    ]

    def test_all_noise_cases_excluded(self) -> None:
        for title in self.NOISE_CASES:
            with self.subTest(title=title):
                self.assertFalse(
                    is_jnet21_target(_jnet21_item(title)),
                    msg=f"noise が allow されている: {title!r}",
                )


class TestIsJnet21TargetSignal(unittest.TestCase):
    """ピスケアにとって signal となるべきケースが allow されることを確認する。"""

    SIGNAL_CASES = [
        # 久留米市の制度
        "【久留米市】令和8年度〇〇補助金",
        "【福岡県久留米市】〇〇補助金",
        # 福岡県の県政
        "【福岡県】補助金・助成金：令和8年度介護DX補助金",
        # 国制度（prefix なし）
        "令和8年度業務改善助成金",
        "デジタル化・AI導入補助金2026 通常枠 2 次締切",
        "事業承継・M&A補助金（15次公募）",
        # 旧 regex 実装で false positive deny されていたパターン
        "市町村連携高齢者訪問看護事業補助金",
        "中央卸売市場機能強化補助金",
    ]

    def test_all_signal_cases_allowed(self) -> None:
        for title in self.SIGNAL_CASES:
            with self.subTest(title=title):
                self.assertTrue(
                    is_jnet21_target(_jnet21_item(title)),
                    msg=f"signal が deny されている: {title!r}",
                )


class TestIsJnet21TargetBodyOnlyPrefecture(unittest.TestCase):
    """Layer 1c: body に他県名がある title を deny することを確認する。"""

    OTHER_PREFECTURE_TITLES = [
        "兵庫県の中小事業者向け補助金",
        "千葉県肥料価格高騰緊急支援事業",
        "東京都DX推進補助金",
    ]

    def test_other_prefecture_in_body_denied(self) -> None:
        for title in self.OTHER_PREFECTURE_TITLES:
            with self.subTest(title=title):
                self.assertFalse(
                    is_jnet21_target(_jnet21_item(title)),
                    msg=f"他県本文の title が allow されている: {title!r}",
                )


class TestIsJnet21TargetUnknownPrefixWithCityBody(unittest.TestCase):
    """Layer 1 が unknown-prefix を返したとき、Layer 1b が body の市町村を deny できることを確認する。"""

    def test_zenkoku_prefix_with_other_city_body_denied(self) -> None:
        # 旧実装では 【全国】 を unknown-prefix で allow → Layer 1b regex が 【 始まりを除外 → leak
        self.assertFalse(is_jnet21_target(_jnet21_item("【全国】古賀市まちづくり補助金")))

    def test_kyougikai_prefix_with_other_city_body_denied(self) -> None:
        self.assertFalse(is_jnet21_target(_jnet21_item("【協議会】福岡市〇〇補助金")))

    def test_zenkoku_prefix_with_kurume_body_allowed(self) -> None:
        # 【全国】 prefix でも body が久留米市なら allow
        self.assertTrue(is_jnet21_target(_jnet21_item("【全国】久留米市〇〇補助金")))


class TestIsJgrantsTargetSlackNoise(unittest.TestCase):
    """jGrants 実観測の noise が deny されることを確認する。"""

    NOISE_CASES = [
        "【福岡県宗像市】令和8年度 宗像市がんばる中小企業者応援補助金",
        "【福岡県宗像市】令和8年度 宗像市創業応援補助金（“宗業”者応援補助金）",
        "【福岡県宗像市】令和8年度 食のまち宗像推進補助金",
        "古賀市温室効果ガス排出量可視化システム導入費補助金",
        "古賀市中小企業等向け太陽光発電設備導入補助金",
        "【福岡市】グリーンビル促進事業（都心部のオフィスビルなどへの緑化助成）",
    ]

    def test_all_noise_cases_excluded(self) -> None:
        for title in self.NOISE_CASES:
            with self.subTest(title=title):
                self.assertFalse(
                    is_jgrants_target(_jgrants_item(title)),
                    msg=f"jGrants noise が allow されている: {title!r}",
                )


class TestIsJgrantsTargetSignal(unittest.TestCase):
    """jGrants で ピスケアにとって signal となるべきケースが allow されることを確認する。"""

    def test_visit_nursing_subsidy_allowed(self) -> None:
        # 訪問看護に直接関連する国制度（実観測）
        self.assertTrue(
            is_jgrants_target(
                _jgrants_item("在宅人工呼吸器使用難病患者非常用電源設備整備事業補助金", area="全国")
            )
        )

    def test_kurume_subsidy_allowed(self) -> None:
        self.assertTrue(is_jgrants_target(_jgrants_item("久留米市WLB 補助金", area="福岡県")))

    def test_fukuoka_prefecture_subsidy_allowed(self) -> None:
        self.assertTrue(
            is_jgrants_target(
                _jgrants_item("【福岡県】補助金・助成金：令和8年度介護DX補助金", area="福岡県")
            )
        )

    def test_area_outside_fukuoka_denied(self) -> None:
        self.assertFalse(is_jgrants_target(_jgrants_item("〇〇補助金", area="北海道")))

    def test_empty_area_falls_through_to_title_filter(self) -> None:
        # area 空の場合は title 判定のみで決まる（stderr 出力されることは別途検証）
        self.assertTrue(
            is_jgrants_target(
                _jgrants_item("令和8年度業務改善助成金", area="")
            )
        )
        self.assertFalse(
            is_jgrants_target(
                _jgrants_item("【熊本県】〇〇補助金", area="")
            )
        )


class TestIsJnet21TargetEastKurumeDefense(unittest.TestCase):
    """東久留米市（東京都）の誤ヒット防御テスト。"""

    def test_east_kurume_with_admin_prefix_denied(self) -> None:
        self.assertFalse(is_jnet21_target(_jnet21_item("【東久留米市】〇〇補助金")))

    def test_east_kurume_composite_denied(self) -> None:
        self.assertFalse(is_jnet21_target(_jnet21_item("【東京都東久留米市】〇〇補助金")))

    def test_east_kurume_in_title_body_denied(self) -> None:
        # 【】 なしでも本文に「東久留米」があれば TITLE_DENY_SUBSTRING で除外
        self.assertFalse(is_jnet21_target(_jnet21_item("東久留米地区連携セミナー助成金")))


if __name__ == "__main__":
    unittest.main()
