"""Admin password policy (authentication hardening, 2026-08-19).

Pure stdlib logic — no imports from ``quota`` (the ``core`` package is the
clean foundation). The dashboard is the single most attractive target once the
box sits on a public IP, so a weak/default admin password must never be
allowed to remain in place. This module is the policy the API enforces on
every password change and the WAN tab consults before enabling Strong WAN
mode: a factory-default ("admin") or policy-violating password BLOCKS WAN
activation outright.

The bundled ``COMMON_PASSWORDS`` set is a curated sample of the real-world
most-guessed passwords (the "you're still vulnerable" tier, not the full
top-10k — the whole point is catching the trivially guessable tier with zero
external calls). It is intentionally ASCII-lowercase; comparison normalizes
the candidate the same way, so case-flipped variants are caught too.
"""

from __future__ import annotations

#: Minimum length for a NEW admin password (change/setup forms).
MIN_LENGTH = 12

#: Character classes the policy requires (digit, upper, lower, symbol).
_REQUIRED_CLASSES = ("lowercase", "uppercase", "digit", "symbol")

#: Common/weak passwords never allowed, regardless of length. ASCII-lowercase;
#: ``policy_violations`` lowercases + strips the candidate before matching.
COMMON_PASSWORDS: frozenset[str] = frozenset({
    # the factory default + near-variants — the single most important entry
    "admin", "administrator", "quota", "quotaadmin", "gateway", "quota-manager",
    # top-tier universally-guessed passwords (source: repeated breach dumps)
    "password", "password1", "password123", "password1234", "password12",
    "123456", "1234567", "12345678", "123456789", "1234567890",
    "12345678910", "12345", "1234", "123", "12", "1",
    "qwerty", "qwerty123", "qwertyuiop", "qwerty1",
    "abc123", "abc1234", "abc", "abcd", "abcdef",
    "letmein", "welcome", "welcome1", "welcome123", "monkey", "monkey123",
    "dragon", "dragon1", "dragon123", "master", "master1", "master123",
    "login", "login123", "princess", "princess1", "football", "football1",
    "baseball", "baseball1", "superman", "superman1", "batman", "batman1",
    "soccer", "soccer1", "footbal", "mustang", "mustang1", "shadow", "shadow1",
    "trustno1", "iloveyou", "iloveyou1", "sunshine", "sunshine1", "starwars",
    "starwars1", "whatever", "whatever1", "hello", "hello1", "hello123",
    "welcome2", "pokemon", "pokemon1", "hunter", "hunter1", "buster",
    "buster1", "george", "george1", "andrew", "andrew1", "charlie",
    "charlie1", "tigger", "tigger1", "dallas", "dallas1", "passw0rd",
    "pass1234", "password0", "default", "default1", "changeme", "changeme1",
    "letmein1", "p@ssword", "p@ssw0rd", "p@ss1234",
    "admin123", "admin1234", "root", "root123", "toor", "system", "system1",
    "test", "test1", "test123", "test1234", "guest", "guest1", "guest123",
    "user", "user1", "user123", "user1234", "temp", "temp1234", "temporary",
    "access", "access1", "secure", "secure1", "security", "s3curity",
    "summer", "summer1", "winter", "winter1", "spring", "spring1",
    "autumn", "autumn1", "summer2020", "summer2021", "summer2022",
    "freedom", "freedom1", "nothing", "nothing1", "whatever123",
    "donald", "donald1", "jordan", "jordan1", "michael", "michael1",
    "michelle", "michelle1", "jessica", "jessica1", "jennifer", "jennifer1",
    "justin", "justin1", "daniel", "daniel1", "matthew", "matthew1",
    "thomas", "thomas1", "robert", "robert1", "richard", "richard1",
    "anthony", "anthony1", "nicholas", "nicholas1", "joshua", "joshua1",
    "kevin", "kevin1", "brandon", "brandon1", "david", "david1", "james",
    "james1", "john", "john1", "john123", "william", "william1", "steven",
    "steven1", "joseph", "joseph1", "tyler", "tyler1", "dustin", "dustin1",
    "chris", "chris1", "christopher", "christopher1", "amber", "amber1",
    "britney", "britney1", "melissa", "melissa1", "sarah", "sarah1",
    "ashley", "ashley1", "laura", "laura1", "stephanie", "stephanie1",
    "kimberly", "kimberly1", "vanessa", "vanessa1", "andrea", "andrea1",
    "pamela", "pamela1", "mary", "mary1", "linda", "linda1", "sandra",
    "sandra1", "karen", "karen1", "nancy", "nancy1", "betty", "betty1",
    "helen", "helen1", "samantha", "samantha1", "debbie", "debbie1",
    "jasmine", "jasmine1", "shawn", "shawn1", "taylor", "taylor1", "jordan23",
    "asdfgh", "asdf", "asdfghjkl", "zxcvbn", "qazwsx", "qwertyuiop123",
    "111111", "000000", "121212", "7777777", "696969", "666666", "888888",
    "777777", "555555", "444444", "333333", "222222", "999999", "100200300",
    "654321", "112233", "321123", "135790", "246810", "987654321",
    "1q2w3e", "1q2w3e4r", "qwe123", "qweasd", "qqqqqq", "aaaaaa", "aaaaaa1",
    "zzzzzz", "aa123456", "google", "google1", "yahoo", "yahoo1", "facebook",
    "facebook1", "instagram", "instagram1", "twitter", "twitter1", "netflix",
    "netflix1", "amazon", "amazon1", "linkedin", "linkedin1", "microsoft",
    "microsoft1", "apple", "apple1", "hotmail", "hotmail1", "gmail",
    "gmail1", "outlook", "outlook1", "orange", "orange1", "purple",
    "purple1", "yellow", "yellow1", "green", "green1", "blue", "blue1",
    "red", "red1", "black", "black1", "white", "white1", "pink", "pink1",
    "silver", "silver1", "gold", "gold1", "rainbow", "rainbow1",
    "computer", "computer1", "internet", "internet1", "hacker", "hacker1",
    "secret", "secret1", "secrets", "mypassword", "mypassword1", "mypass123",
    "passwordpassword", "password1!", "password!123", "qwerty!23",
    "scooby", "scooby1", "cheese", "cheese1", "pepper", "pepper1",
    "cookie", "cookie1", "cookie123", "monster", "monster1",
    "fluffy", "fluffy1", "mittens", "mittens1", "snoopy", "snoopy1",
    "garfield", "garfield1", "spongebob", "spongebob1", "ninja", "ninja1",
    "mario", "mario1", "luigi", "luigi1", "peanut", "peanut1", "banana",
    "banana1", "apple123", "orange123", "flower", "flower1", "happiness",
    "happiness1", "forever", "forever1", "jacket", "jacket1", "liverpool",
    "liverpool1", "arsenal", "arsenal1", "chelsea", "chelsea1", "crystal",
    "crystal1", "angela", "angela1", "tiffany", "tiffany1", "bailey",
    "bailey1", "maxwell", "maxwell1", "jesus", "jesus1", "christ",
    "christ1", "satan", "satan1", "money", "money1", "money123",
    "goldfish", "goldfish1", "1qaz2wsx", "zaq12wsx", "1qazxsw2",
    "qwerty12345", "admin2020", "admin2021", "admin2022", "admin2023",
    "password2020", "password2021", "password2022", "changeme123",
    "letmein123", "welcome!23", "Welcome1", "Abc12345", "Admin12345",
    "P@ssword1", "Passw0rd!", "Passw0rd123",
})


def normalize(password: str) -> str:
    """Lowercase + strip whitespace for the common-password comparison."""
    return password.strip().lower()


def policy_violations(password: str) -> list[str]:
    """Return every policy violation as a human-readable message.

    Empty list = the password is acceptable. The API surfaces these messages
    verbatim to the admin, so they read like actionable guidance rather than a
    bare rejection.
    """
    violations: list[str] = []
    if not password or len(password) < MIN_LENGTH:
        violations.append(
            f"at least {MIN_LENGTH} characters")
    if not any(ch.islower() for ch in password):
        violations.append("at least one lowercase letter")
    if not any(ch.isupper() for ch in password):
        violations.append("at least one uppercase letter")
    if not any(ch.isdigit() for ch in password):
        violations.append("at least one digit")
    if not any(not ch.isalnum() for ch in password):
        violations.append("at least one symbol (e.g. ! @ # $)")
    if normalize(password) in COMMON_PASSWORDS:
        violations.append(
            "a commonly-guessed password (the bundled common-password list)")
    return violations


def is_compliant(password: str) -> bool:
    """True when the password passes every policy check."""
    return not policy_violations(password)
