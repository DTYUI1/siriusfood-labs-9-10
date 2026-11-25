"""Bounded local relational plan and public-only compiler.

Raw spans exist only during parsing. Plans retain offsets/roles, enums, trusted
SQL ids and normalized local numbers; the compiler never copies user fragments.
This grammar does not claim universal Russian understanding.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    role: str


@dataclass(frozen=True)
class Context:
    relation: str
    topic: str


@dataclass(frozen=True)
class Constraint:
    operator: str
    value: Decimal
    scope: str


@dataclass(frozen=True)
class PublicRequest:
    target: str
    operation: str
    entity_ids: tuple[int, ...]
    constraints: tuple[Constraint, ...] = ()
    context: tuple[Context, ...] = ()
    explicit_entities: bool = True


@dataclass(frozen=True)
class PublicQuestionPlan:
    requests: tuple[PublicRequest, ...] = ()
    trace: tuple[Span, ...] = ()
    clarification: str = ""


@dataclass(frozen=True)
class PublicQuery:
    question: str
    source_ids: tuple[str, ...]
    require_all: bool = True


@dataclass(frozen=True)
class CompiledQuestion:
    question: str = ""
    source_ids: tuple[str, ...] = ()
    queries: tuple[PublicQuery, ...] = ()
    clarification: str = ""


CLARIFY = "Уточни: нужна цена блюда, подбор по бюджету или порядок оплаты?"
TEMPORAL = re.compile(r"\b(до|перед|после)\s+((?:подтвержд[а-я]*\s*)?(?:оплат[а-я]*|платеж[а-я]*|подтвержд[а-я]*|достав[а-я]*|отмен[а-я]*))")
NUMBER_WORDS = {}
for value, forms in [
    (0,'ноль'),(1,'один одного одну'),(2,'два две двух'),(3,'три трех'),(4,'четыре четырех'),
    (5,'пять пяти'),(6,'шесть шести'),(7,'семь семи'),(8,'восемь восьми'),(9,'девять девяти'),
    (10,'десять десяти'),(11,'одиннадцать одиннадцати'),(12,'двенадцать двенадцати'),
    (13,'тринадцать тринадцати'),(14,'четырнадцать четырнадцати'),(15,'пятнадцать пятнадцати'),
    (16,'шестнадцать шестнадцати'),(17,'семнадцать семнадцати'),(18,'восемнадцать восемнадцати'),
    (19,'девятнадцать девятнадцати'),(20,'двадцать двадцати'),(30,'тридцать тридцати'),
    (40,'сорок сорока'),(50,'пятьдесят пятидесяти'),(60,'шестьдесят шестидесяти'),
    (70,'семьдесят семидесяти'),(80,'восемьдесят восьмидесяти'),(90,'девяносто девяноста'),
    (100,'сто ста'),(200,'двести двухсот'),(300,'триста трехсот'),(400,'четыреста четырехсот'),
    (500,'пятьсот пятисот'),(600,'шестьсот шестисот'),(700,'семьсот семисот'),
    (800,'восемьсот восьмисот'),(900,'девятьсот девятисот'),
]:
    NUMBER_WORDS.update({word: value for word in forms.split()})
NUMBER = r"(?:\d{1,4}(?:[.,]\d{1,2})?|(?:" + '|'.join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")(?:\s+(?:" + '|'.join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")){0,2})"
MONEY = re.compile(r"(?P<relation>\bдо|дешевле|не\s+(?:дороже|выше)|в\s+пределах|бюджет(?:ом)?(?:\s+до)?|\bот|\bза)\s*(?P<number>" + NUMBER + r")\s*(?:₽|руб[а-я]*|р\.?)?")
ATTRIBUTE = re.compile(r"\b(?:стоимостью|ценой)\s*(?P<number>" + NUMBER + r")\s*(?:₽|руб[а-я]*|р\.?)?")


def number_value(value):
    if re.fullmatch(r"\d{1,4}(?:[.,]\d{1,2})?", value):
        return Decimal(value.replace(',', '.'))
    numbers = [NUMBER_WORDS[word] for word in value.split()]
    categories = [100 if v >= 100 else 10 if v >= 10 else 1 for v in numbers]
    if categories != sorted(set(categories), reverse=True):
        raise InvalidOperation()
    return Decimal(sum(numbers))


def _input_number(value):
    # Catch only malformed input conversion. Invalid public SQL prices or other
    # programming/dependency failures must not be disguised as an input hint.
    try:
        return number_value(value)
    except InvalidOperation:
        return None


def _mask(text, spans):
    chars = list(text)
    for span in spans:
        chars[span.start:span.end] = ' ' * (span.end - span.start)
    return ''.join(chars)


def _stem(word):
    return word[:max(3 if len(word) <= 4 else 4, len(word) - 2)]


def _entities(text, menu):
    """Resolve dish heads, not any shared ingredient word, to trusted SQL ids."""
    candidates = []
    for item in menu:
        words = re.findall(r'[а-яa-z]{3,}', item['name'].casefold().replace('ё', 'е'))
        if not words:
            continue
        matches = [re.search(r'(?<![а-яa-z])' + re.escape(_stem(word)), text) for word in words]
        if matches[0]:
            candidates.append((item['id'], sum(m is not None for m in matches), _stem(words[0])))
    if len(candidates) > 1 and len({c[2] for c in candidates}) == 1:
        best = max(c[1] for c in candidates)
        candidates = [c for c in candidates if c[1] == best]
        if len(candidates) > 1:
            return (), True
    if not candidates:
        categories = {item.get('category_name') for item in menu if item.get('category_name')}
        matched = [name for name in categories if any(re.search(r'(?<![а-яa-z])' + re.escape(_stem(word)), text)
                   for word in re.findall(r'[а-яa-z]{3,}', name.casefold().replace('ё', 'е')))]
        if matched:
            return tuple(item['id'] for item in menu if item.get('category_name') in matched), False
    return tuple(c[0] for c in candidates), False


def _unresolved_object(focus, ids, menu):
    if ids:
        return False
    # Unknown direct object cannot silently become the whole menu. Ignore only
    # known grammar/filler/category tokens; residuals never become outbound text.
    known = ('покаж','подбер','посовет','предлож','выбр','взять','мож','можн','что','чем','есть',
             'нибудь','обед','блюд','меню','горяч','напит','суп','цен','стоим','скольк','денег',
             'деньг','сегод','сейчас','корм','поесть','покуш','перекус','пообед','предлага',
             'доступ','расскаж','пожалуй','куп','нужн','хоч','мне','может','приобр')
    for token in re.findall(r'[а-яa-z]{3,}', focus):
        if not token.startswith(known) and token not in ('для', 'или', 'как', 'где', 'лишь'):
            return True
    return False


def plan_public_question(text, menu):
    """Attach temporal/attribute phrases before identifying each requested head."""
    boundaries = list(re.finditer(r'\s+и\s+(?=(?:как|сколько|где|можно|почему|узнать|посмотреть|отменить|оплатить)\b)', text))
    if len(boundaries) > 1:
        return PublicQuestionPlan(clarification=CLARIFY)
    clauses, cursor = [], 0
    for boundary in boundaries:
        clauses.append((text[cursor:boundary.start()], cursor))
        cursor = boundary.end()
    clauses.append((text[cursor:], cursor))
    requests, trace = [], []
    antecedent = ()
    for clause, offset in clauses:
        spans, contexts, constraints = [], [], []
        for match in TEMPORAL.finditer(clause):
            relation = {'до': 'before', 'перед': 'before', 'после': 'after'}[match.group(1)]
            topic = ('delivery' if 'достав' in match.group(2) else 'cancellation' if 'отмен' in match.group(2)
                     else 'payment_confirmation' if 'подтвержд' in match.group(2) else 'payment')
            spans.append(Span(match.start(), match.end(), 'temporal_context'))
            contexts.append(Context(relation, topic))
        for match in ATTRIBUTE.finditer(clause):
            value = _input_number(match.group('number'))
            if value is None:
                return PublicQuestionPlan(clarification='Не удалось разобрать сумму. Укажи бюджет числом в рублях.')
            spans.append(Span(match.start(), match.end(), 'entity_attribute'))
            constraints.append(Constraint('equal', value, 'entity_attribute'))
        remaining = _mask(clause, spans)
        amount_ambiguity = None
        for match in MONEY.finditer(remaining):
            value = _input_number(match.group('number'))
            if value is None:
                return PublicQuestionPlan(clarification='Не удалось разобрать сумму. Укажи бюджет числом в рублях.')
            relation = match.group('relation')
            operator = 'min' if relation == 'от' else 'less_than' if relation == 'дешевле' else 'max'
            if match.group('relation') == 'за':
                amount_ambiguity = value
            constraints.append(Constraint(operator, value, 'selection_filter'))
            spans.append(Span(match.start(), match.end(), 'selection_constraint'))
        focus = _mask(clause, spans)
        ids, ambiguous_entity = _entities(clause, menu)
        explicit_entities = bool(ids)
        if ambiguous_entity:
            return PublicQuestionPlan(clarification='Уточни полное название блюда.')
        if not ids and antecedent and re.search(r'\b(?:ее|его|их|это)\b', focus):
            ids = antecedent
        price = re.search(r'сто(?:ит|ят)|\bцен[ауы]|\bстоимость[ьи]?|обойд|сколько.*ден|нужн.*ден', focus)
        payment = re.search(r'оплат|оплач|списан|списыв|подтвержд|подтверд|оформ', focus)
        cancel = re.search(r'отмен|возврат|отказ|передум', focus)
        delivery = re.search(r'достав|курьер|привез|привоз|принес|получ(?:ить|у|им|ают|ишь).*(?:еду|обед|блюд|заказ)', focus)
        money_issue = re.search(r'(?:деньг|денег).{0,30}(?:исчез|снял|ушл|пропал)|(?:исчез|снял|ушл|пропал).{0,30}(?:деньг|денег)', focus)
        selection = re.search(r'покаж|подбер|посовет|предлож|доступ|есть\b|меню|блюд|на обед|поесть|горячее|категори|кормят|кормите|покушать|перекус|пообедать|предлагаете|расскаж', focus)
        if delivery and re.search(r'стоим|стоит|цен|плат[ауы]|платн|бесплат|сколько|ждат|когда|врем|минут|через.{0,8}час', focus):
            return PublicQuestionPlan()
        # Both heads in one clause are genuinely unresolved, not priority-ranked.
        if price and (payment or cancel):
            return PublicQuestionPlan(clarification=CLARIFY)
        if cancel:
            target = 'rules.cancel'
        elif payment or money_issue:
            target = 'rules.payment'
        elif price:
            target = 'menu.price'
        elif delivery:
            target = 'rules.delivery'
        elif constraints or selection or (ids and len(re.findall(r'[а-яa-z]+', focus)) <= 2):
            target = 'menu.selection'
        else:
            return PublicQuestionPlan(clarification=CLARIFY)
        if target.startswith('menu.'):
            if _unresolved_object(focus, ids, menu):
                return PublicQuestionPlan(clarification='Уточни название блюда из опубликованного меню.')
            if amount_ambiguity is not None:
                return PublicQuestionPlan(clarification=f'Уточни: подобрать обед не дороже {amount_ambiguity:g} рублей?')
            if re.search(r'\b(?:до|дороже|пределах|бюджет)\b', focus) and not constraints:
                return PublicQuestionPlan(clarification='Уточни бюджет числом в рублях.')
            eligible = [item for item in menu if not ids or item['id'] in ids]
            for constraint in constraints:
                if constraint.scope != 'selection_filter':
                    continue
                eligible = [item for item in eligible if item['is_available'] and
                            (Decimal(str(item['price'])) >= constraint.value if constraint.operator == 'min'
                             else Decimal(str(item['price'])) < constraint.value if constraint.operator == 'less_than'
                             else Decimal(str(item['price'])) <= constraint.value)]
            if not eligible:
                return PublicQuestionPlan()
            ids = tuple(item['id'] for item in eligible[:5])
        else:
            # Amount attached to an operation is context, not a claim of eligibility.
            constraints = [Constraint(c.operator, c.value, 'context_only') for c in constraints]
        operation = 'explain' if target.startswith('rules.') else 'lookup' if target == 'menu.price' else 'select'
        head = {'rules.cancel': cancel, 'rules.payment': payment or money_issue,
                'rules.delivery': delivery, 'menu.price': price, 'menu.selection': selection}.get(target)
        if head:
            spans.append(Span(head.start(), head.end(), 'requested_' + target))
        requests.append(PublicRequest(target, operation, ids, tuple(constraints), tuple(contexts), explicit_entities))
        trace.extend(Span(s.start + offset, s.end + offset, s.role) for s in spans)
        antecedent = ids or antecedent
    return PublicQuestionPlan(tuple(requests), tuple(trace))


def compile_public_question(plan, menu):
    if plan.clarification or not plan.requests:
        return CompiledQuestion(clarification=plan.clarification)
    by_id = {item['id']: item for item in menu}
    rules = {
        'rules.payment': ('rules-payment', 'Как оформляется и подтверждается оплата заказа?'),
        'rules.cancel': ('rules-cancel', 'Каковы правила отмены заказа и возврата?'),
        'rules.delivery': ('rules-delivery', 'Как устроено назначение курьера и доставка без гарантии срока?'),
    }
    queries, questions, sources = [], [], []
    for request in plan.requests:
        if request.target in rules:
            source, question = rules[request.target]
            query = PublicQuery(question, (source,))
            if request.constraints:
                question += ' Запрошены правила действия; не утверждай соответствие блюда указанному бюджету.'
        else:
            names = ', '.join(by_id[i]['name'] for i in request.entity_ids)
            question = 'Какова цена и доступность блюд: ' + names + '?'
            query = PublicQuery(question, tuple(f'menu-{i}' for i in request.entity_ids),
                                request.target == 'menu.price' and request.explicit_entities)
        for context in request.context:
            relation = 'до' if context.relation == 'before' else 'после'
            topic = {'payment': 'оплаты', 'payment_confirmation': 'подтверждения оплаты', 'delivery': 'доставки', 'cancellation': 'отмены'}[context.topic]
            question += f' Контекст: {relation} {topic}; отвечай на запрошенный вопрос, не придумывай навигацию интерфейса.'
        queries.append(query);questions.append(question);sources.extend(query.source_ids)
    return CompiledQuestion(' '.join(questions), tuple(dict.fromkeys(sources)), tuple(queries))
