"""~200 search keywords for a Japanese restaurant in Pune, with a DRAFT gold grouping.

STATUS: pending_user_approval. Drafted by the backend agent (D-B7-3) BEFORE the grouping was
scored, and not independent of it: a person must review GOLD and approve or edit it before any
score is quoted as a result. Hinglish and typos are deliberate.
"""
STATUS = "pending_user_approval"

GOLD: dict[str, list[str]] = {
    "sushi_delivery": [
        "sushi delivery pune", "sushi home delivery", "sushi delivry pune", "order sushi online",
        "sushi online order pune", "sushi ghar pe delivery", "sushi swiggy pune", "sushi zomato",
        "sushi takeaway pune", "sushii delivery", "sushi delivery kalyani nagar",
        "sushi delivery baner", "best sushi delivery pune", "sushi home delivery kothrud",
        "shushi delivery"],
    "sushi_near_me": [
        "sushi near me", "sushi nearby", "sushi restaurant near me", "sushi places near me",
        "sushi paas mein", "sushi near by", "sushi aas paas", "best sushi near me",
        "sushi near me open now", "sushi close to me", "sushie near me"],
    "sushi_area": [
        "sushi koregaon park", "sushi in baner", "sushi viman nagar", "sushi aundh",
        "sushi restaurant kalyani nagar", "sushi hinjewadi", "sushi fc road", "sushi wakad",
        "sushi magarpatta", "sushi kp pune", "sushi camp pune", "sushi kothrud"],
    "sushi_price": [
        "sushi price pune", "sushi platter price", "cheap sushi pune", "sushi sasta",
        "sushi rate pune", "sushi cost", "affordable sushi pune", "sushi under 500",
        "sushi offers pune", "sushi discount", "budget sushi"],
    "sushi_info": [
        "what is sushi", "sushi kaise khate hai", "sushi calories", "sushi meaning",
        "how to eat sushi", "sushi kya hai", "sushi vs sashimi", "sushi recipe",
        "difference between sushi and maki", "how to make sushi at home"],
    "ramen_delivery": [
        "ramen delivery pune", "ramen home delivery", "order ramen online", "ramen swiggy",
        "ramen zomato pune", "ramen ghar pe", "raman delivery pune", "ramen takeaway",
        "ramen delivery viman nagar", "ramen online order"],
    "ramen_area": [
        "ramen koregaon park", "ramen baner", "ramen viman nagar", "ramen aundh",
        "ramen kalyani nagar", "ramen fc road", "ramen hinjewadi", "ramen camp"],
    "ramen_generic": [
        "best ramen pune", "ramen pune", "ramen restaurant pune", "top ramen in pune",
        "japanese ramen pune", "good ramen pune", "ramen bowl pune", "ramen noodles pune",
        "tonkotsu ramen pune", "spicy ramen pune"],
    "japanese_area": [
        "japanese restaurant koregaon park", "japanese restaurant baner",
        "japanese food viman nagar", "japanese restaurant kalyani nagar",
        "japanese restaurant aundh", "japanese food hinjewadi", "japanese restaurant fc road",
        "japanese cuisine koregaon park", "japanese restaurant kp", "japanese food camp",
        "japnese restaurant baner", "japanese restaurant magarpatta"],
    "japanese_near_me": [
        "japanese restaurant near me", "japanese food near me", "japanese restaurant nearby",
        "japanese food paas mein", "japanese restaurants near me open now",
        "japanese cuisine near me", "japanese restaurant near by", "japanese food aas paas",
        "best japanese restaurant near me", "japanise food near me"],
    "japanese_generic": [
        "japanese restaurant pune", "best japanese restaurant pune", "japanese food pune",
        "top japanese restaurants in pune", "japanese cuisine pune", "authentic japanese pune",
        "good japanese food pune", "japanese restaurant", "japanese dining pune",
        "japani khana pune", "japanese restaurants pune", "fine dining japanese pune"],
    "japanese_delivery": [
        "japanese food delivery pune", "japanese food home delivery",
        "order japanese food online", "japanese food swiggy", "japanese food zomato",
        "japanese takeaway pune", "japanese food ghar pe delivery", "japanese food delivry"],
    "brand_hana": [
        "hana japanese restaurant", "hana restaurant pune", "hana menu", "hana pune",
        "hana japanese pune reviews", "hana restaurant koregaon park", "hana booking",
        "hana japanese menu price", "hana contact number", "hana timings"],
    "bento_delivery": [
        "bento box delivery pune", "bento delivery", "order bento online", "bento swiggy",
        "bento box home delivery", "bento lunch delivery pune", "bento zomato"],
    "teppanyaki_price": [
        "teppanyaki price pune", "teppanyaki buffet price", "teppanyaki cost",
        "teppanyaki rate", "cheap teppanyaki pune", "teppanyaki offers", "teppanyaki discount"],
    "japanese_info": [
        "what is japanese food", "japanese food kya hai", "japanese food calories",
        "how to use chopsticks", "japanese cuisine meaning", "what is teppanyaki",
        "what is bento", "tempura recipe", "what is ramen", "gyoza recipe",
        "udon vs ramen", "what is katsu"],
    "dishes_generic": [
        "tempura pune", "gyoza pune", "udon pune", "katsu pune", "donburi pune",
        "dim sum pune", "best tempura pune", "gyoza restaurant pune", "katsu curry pune",
        "udon noodles pune", "teppanyaki pune", "bento box pune"],
}

KEYWORDS = [k for ks in GOLD.values() for k in ks]
LABEL = {k: g for g, ks in GOLD.items() for k in ks}
