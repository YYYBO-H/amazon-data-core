from amazon_data_core.rules import scoped_rule_code


def test_scoped_rule_codes_are_stable_and_bounded():
    code = scoped_rule_code(
        "FRESH",
        "advertising_purchased_products",
        "amazon_ads_purchased_products_store-very-long_marketplace-long",
    )
    assert len(code) <= 60
    assert code == scoped_rule_code(
        "FRESH",
        "advertising_purchased_products",
        "amazon_ads_purchased_products_store-very-long_marketplace-long",
    )
