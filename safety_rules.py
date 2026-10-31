"""Small, shared checks for reported food-safety symptoms and mold."""

import re


_MOLD_REPORT = re.compile(r'곰팡이|부패|썩(?:었|은|음)|상했')
_MOLD_ABSENCE = re.compile(
    r'\s*(?:(?:가|는|은|이|도|을|를)\s*)?'
    r'(?:없|아니|아닌|아님|안\s*(?:보|생기|피)|'
    r'(?:생기|피|발생하)지\s*(?:않|못)|예방|방지)')
_SYMPTOM = re.compile(
    r'복통|구토|토(?:해|했|하|함)|설사|두드러기|아프|아파|'
    r'몸.{0,4}이상|속(?:이|도)?\s*(?:안\s*좋|메스껍|울렁)')
_SYMPTOM_ABSENCE = re.compile(
    r'\s*(?:(?:이|가|은|는|도)\s*)?(?:없|아니|안\s*했|지(?:도)?\s*(?:않|못))')
_NEGATIVE_PREFIX = re.compile(r'(?:^|[^가-힣])(?:안|못)\s*$')


def has_reported_mold(text):
    return any(not _MOLD_ABSENCE.match(text[match.end():])
               for match in _MOLD_REPORT.finditer(text))


def has_reported_symptom(text):
    for match in _SYMPTOM.finditer(text):
        if _NEGATIVE_PREFIX.search(text[max(0, match.start() - 6):match.start()]):
            continue
        if _SYMPTOM_ABSENCE.match(text[match.end():]):
            continue
        return True
    return False
