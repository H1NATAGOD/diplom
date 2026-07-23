from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

MIN_QTY = MinValueValidator(Decimal("0.001"))
MIN_PRICE = MinValueValidator(Decimal("0.00"))


def parse_percent_or_amount(value: str, base_amount: Decimal) -> Decimal:
    raw = (value or "").strip().replace(",", ".")
    if not raw:
        return Decimal("0.00")
    try:
        if raw.endswith("%"):
            pct = Decimal(raw[:-1])
            return (base_amount * pct / Decimal("100")).quantize(Decimal("0.01"))
        return Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValidationError("Непредвиденные расходы должны быть числом или процентом, например '10%'") from exc


class CounterpartyType(models.Model):
    name = models.CharField(max_length=60, verbose_name="Тип контрагента")

    class Meta:
        verbose_name = "Тип контрагента"
        verbose_name_plural = "Типы контрагентов"

    def __str__(self) -> str:
        return self.name


class Counterparty(models.Model):
    name = models.CharField(max_length=60, verbose_name="Наименование/ФИО")
    inn = models.CharField(max_length=60, verbose_name="ИНН")
    ogrn = models.CharField(max_length=60, verbose_name="ОГРН")
    kpp = models.CharField(max_length=60, verbose_name="КПП")
    legal_entities = models.CharField(max_length=60, verbose_name="ОПФ")
    legal_address = models.CharField(max_length=60, verbose_name="Юр. адрес")
    telephone_number = models.CharField(max_length=60, verbose_name="Телефон")
    email = models.EmailField(max_length=60)
    current_account = models.CharField(max_length=60, verbose_name="Расчетный счет")

    class Meta:
        verbose_name = "Контрагент"
        verbose_name_plural = "Контрагенты"

    def __str__(self) -> str:
        return self.name


class Contract(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Черновик"
        SIGNED = "SIGNED", "Подписан"
        IN_WORK = "IN_WORK", "В работе"
        CLOSED = "CLOSED", "Закрыт"
        CANCELLED = "CANCELLED", "Отменен"

    contract_number = models.CharField(max_length=60, verbose_name="Номер договора")
    name = models.CharField(max_length=60, verbose_name="Наименование")
    conclusion_date = models.DateTimeField(default=timezone.now, verbose_name="Дата заключения")
    status = models.CharField(max_length=60, choices=Status.choices, default=Status.DRAFT, verbose_name="Статус")
    counterparty = models.ForeignKey(Counterparty, on_delete=models.CASCADE)
    classification = models.ForeignKey(CounterpartyType, on_delete=models.PROTECT)

    class Meta:
        verbose_name = "Договор"
        verbose_name_plural = "Договоры"

    def can_transition_to(self, next_status: str) -> bool:
        allowed = {
            self.Status.DRAFT: {self.Status.SIGNED, self.Status.CANCELLED},
            self.Status.SIGNED: {self.Status.IN_WORK, self.Status.CANCELLED},
            self.Status.IN_WORK: {self.Status.CLOSED, self.Status.CANCELLED},
            self.Status.CLOSED: set(),
            self.Status.CANCELLED: set(),
        }
        return next_status in allowed.get(self.status, set())

    def set_status(self, next_status: str) -> None:
        if not self.can_transition_to(next_status):
            raise ValidationError("Недопустимый переход статуса договора.")
        self.status = next_status
        self.save(update_fields=["status"])


class Project(models.Model):
    PIPELINE_STATUSES = (
        ("LEAD", "Лид"),
        ("ESTIMATE", "Смета"),
        ("NEGOTIATION", "Переговоры"),
        ("CONTRACTED", "Договор"),
        ("IN_PROGRESS", "В работе"),
        ("DONE", "Завершен"),
        ("LOST", "Потерян"),
    )

    contract = models.ForeignKey(Contract, on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=100, verbose_name="Название объекта")
    address = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=50, choices=PIPELINE_STATUSES, default="LEAD")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = "Проект"
        verbose_name_plural = "Проекты"

    def __str__(self) -> str:
        return self.name


class InteractionHistory(models.Model):
    INTERACTION_TYPES = (
        ("CALL", "Звонок"),
        ("EMAIL", "Email"),
        ("MEETING", "Встреча"),
        ("MESSAGE", "Сообщение"),
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="interactions")
    interaction_type = models.CharField(max_length=20, choices=INTERACTION_TYPES)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "История взаимодействия"
        verbose_name_plural = "История взаимодействий"
        ordering = ["-created_at"]


class Estimate(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="estimates")
    name = models.CharField(max_length=120, verbose_name="Название сметы")
    date = models.DateTimeField(auto_now_add=True)
    unforeseen_expenses = models.CharField(max_length=60, verbose_name="Непредвиденные расходы", default="0")
    profit = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        verbose_name="Планируемая прибыль",
        default=Decimal("0.00"),
        validators=[MIN_PRICE],
    )
    total_cost = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = "Смета"
        verbose_name_plural = "Сметы"
        ordering = ["-date", "-id"]

    def __str__(self) -> str:
        return self.name

    def direct_total(self) -> Decimal:
        return self.items.filter(type=CostItem.COST_DIRECT).aggregate(total=Sum("price"))["total"] or Decimal("0.00")

    def overhead_total(self) -> Decimal:
        return self.items.filter(type=CostItem.COST_OVERHEAD).aggregate(total=Sum("price"))["total"] or Decimal("0.00")

    def material_total(self) -> Decimal:
        return self.material_items.aggregate(total=Sum("total_price"))["total"] or Decimal("0.00")

    def calculate_total(self) -> Decimal:
        subtotal = self.direct_total() + self.overhead_total() + self.material_total() + (self.profit or Decimal("0.00"))
        unforeseen = parse_percent_or_amount(self.unforeseen_expenses, subtotal)
        return (subtotal + unforeseen).quantize(Decimal("0.01"))

    def recalc_total(self) -> Decimal:
        self.total_cost = self.calculate_total()
        self.save(update_fields=["total_cost"])
        return self.total_cost


class CostItem(models.Model):
    COST_DIRECT = "DIRECT"
    COST_OVERHEAD = "OVERHEAD"
    COST_TYPES = ((COST_DIRECT, "Прямые"), (COST_OVERHEAD, "Накладные"))

    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name="items")
    type = models.CharField(max_length=10, choices=COST_TYPES)
    service_name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=15, decimal_places=2, validators=[MIN_PRICE])

    class Meta:
        verbose_name = "Статья затрат"
        verbose_name_plural = "Статьи затрат"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.estimate.recalc_total()

    def delete(self, *args, **kwargs):
        estimate = self.estimate
        result = super().delete(*args, **kwargs)
        estimate.recalc_total()
        return result


class Material(models.Model):
    name = models.CharField(max_length=120, unique=True, verbose_name="Наименование")
    unit = models.CharField(max_length=20, default="шт", verbose_name="Ед. изм.")
    min_stock = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=Decimal("0.000"),
        verbose_name="Минимальный остаток",
        validators=[MIN_PRICE],
    )

    class Meta:
        verbose_name = "Материал"
        verbose_name_plural = "Материалы"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class MaterialPrice(models.Model):
    material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name="prices")
    supplier = models.ForeignKey(Counterparty, on_delete=models.SET_NULL, null=True, blank=True)
    price = models.DecimalField(max_digits=15, decimal_places=2)
    valid_from = models.DateField(default=timezone.localdate)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-valid_from", "-id"]


class EstimateMaterialItem(models.Model):
    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name="material_items")
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=15, decimal_places=3, validators=[MIN_QTY])
    unit_price = models.DecimalField(max_digits=15, decimal_places=2, validators=[MIN_PRICE])
    total_price = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0.00"))

    def save(self, *args, **kwargs):
        self.total_price = (self.quantity * self.unit_price).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)
        self.estimate.recalc_total()

    def delete(self, *args, **kwargs):
        estimate = self.estimate
        result = super().delete(*args, **kwargs)
        estimate.recalc_total()
        return result


class Worker(models.Model):
    first_name = models.CharField(max_length=60)
    second_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    passport = models.CharField(max_length=60)
    age_date = models.CharField(max_length=60)
    inn = models.CharField(max_length=60)
    snils = models.CharField(max_length=60)

    def __str__(self) -> str:
        return f"{self.last_name} {self.first_name}"


class Brigade(models.Model):
    name = models.CharField(max_length=60)
    specific = models.CharField(max_length=60, verbose_name="Специализация")
    workers = models.ManyToManyField(Worker, verbose_name="Состав бригады")

    def __str__(self) -> str:
        return self.name


class Technique(models.Model):
    name = models.CharField(max_length=60, verbose_name="Название техники")
    worker = models.ForeignKey(Worker, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Закрепленный рабочий")
    brigade = models.ForeignKey(Brigade, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Бригада")
    end_date = models.DateField(null=True, blank=True, verbose_name="Дата окончания работы")

    def __str__(self) -> str:
        return self.name


class Task(models.Model):
    STATUS_CHOICES = (("TODO", "В планах"), ("IN_PROGRESS", "В работе"), ("DONE", "Завершено"))

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks", verbose_name="Объект")
    name = models.CharField(max_length=200, verbose_name="Название задачи")
    description = models.TextField(blank=True, verbose_name="Описание")
    start_date = models.DateField(verbose_name="Дата начала")
    end_date = models.DateField(verbose_name="Дата окончания")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="TODO", verbose_name="Статус")
    responsible_brigade = models.ForeignKey(
        Brigade,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Ответственная бригада",
    )

    class Meta:
        verbose_name = "Задача"
        verbose_name_plural = "Задачи"

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError("Дата окончания не может быть раньше даты начала.")

    @property
    def progress(self) -> int:
        if self.status == "DONE":
            return 100
        if self.status == "IN_PROGRESS":
            return 50
        return 0

    def __str__(self) -> str:
        return f"{self.name} ({self.project.name})"


class TaskDependency(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="dependencies")
    depends_on = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="blocked_tasks")

    class Meta:
        unique_together = ("task", "depends_on")

    def clean(self):
        if self.task_id == self.depends_on_id:
            raise ValidationError("Задача не может зависеть от самой себя.")


class TaskResource(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="resources")
    worker = models.ForeignKey(Worker, on_delete=models.SET_NULL, null=True, blank=True)
    brigade = models.ForeignKey(Brigade, on_delete=models.SET_NULL, null=True, blank=True)
    technique = models.ForeignKey(Technique, on_delete=models.SET_NULL, null=True, blank=True)
    planned_hours = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))

    def clean(self):
        if not any([self.worker_id, self.brigade_id, self.technique_id]):
            raise ValidationError("Для ресурса нужно выбрать сотрудника, бригаду или технику.")


class Warehouse(models.Model):
    name = models.CharField(max_length=80, unique=True)
    address = models.CharField(max_length=255, blank=True, default="")

    def __str__(self) -> str:
        return self.name


class WarehouseStock(models.Model):
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name="stocks")
    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=15, decimal_places=3, default=Decimal("0.000"), validators=[MIN_PRICE])

    class Meta:
        unique_together = ("warehouse", "material")
        verbose_name = "Остаток на складе"
        verbose_name_plural = "Остатки на складах"


class MaterialReservation(models.Model):
    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name="reservations")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name="reservations")
    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=15, decimal_places=3, default=Decimal("0.000"))
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="material_reservations",
    )

    class Meta:
        unique_together = ("estimate", "warehouse", "material")
        verbose_name = "Резерв материала"
        verbose_name_plural = "Резервы материалов"

    def __str__(self) -> str:
        return f"{self.material} × {self.quantity} ({self.estimate_id})"


class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        INBOUND = "INBOUND", "Приход"
        OUTBOUND = "OUTBOUND", "Расход"
        ADJUSTMENT = "ADJUSTMENT", "Корректировка"
        RESERVE = "RESERVE", "Резерв"
        RELEASE_RESERVE = "RELEASE_RESERVE", "Снятие резерва"
        PURCHASE_RECEIPT = "PURCHASE_RECEIPT", "Оприходование по заявке"

    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, related_name="movements")
    material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name="movements")
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    movement_type = models.CharField(max_length=20, choices=MovementType.choices)
    balance_after = models.DecimalField(max_digits=15, decimal_places=3)
    reserved_after = models.DecimalField(max_digits=15, decimal_places=3, default=Decimal("0.000"))
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    estimate = models.ForeignKey(Estimate, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_movements")
    purchase_request = models.ForeignKey(
        "PurchaseRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Движение по складу"
        verbose_name_plural = "Движения по складу"

    def __str__(self) -> str:
        return f"{self.get_movement_type_display()} {self.material} {self.quantity}"


class PurchaseRequest(models.Model):
    STATUS_CHOICES = (
        ("DRAFT", "Черновик"),
        ("APPROVED", "Согласовано"),
        ("ORDERED", "Заказано"),
        ("RECEIVED", "Получено"),
        ("CANCELLED", "Отменено"),
    )
    estimate = models.ForeignKey(Estimate, on_delete=models.SET_NULL, null=True, blank=True)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="DRAFT")
    created_at = models.DateTimeField(default=timezone.now)
    stock_posted = models.BooleanField(default=False, verbose_name="Оприходовано на склад")

    @classmethod
    @transaction.atomic
    def create_from_estimate(cls, estimate: Estimate, warehouse: Warehouse | None = None) -> "PurchaseRequest":
        from .warehouse_service import available_quantity

        pr = cls.objects.create(estimate=estimate, warehouse=warehouse)
        for item in estimate.material_items.select_related("material"):
            deficit_qty = item.quantity
            if warehouse:
                free = available_quantity(warehouse, item.material)
                deficit_qty = max(Decimal("0.000"), item.quantity - free)
            if deficit_qty > 0:
                PurchaseRequestItem.objects.create(
                    purchase_request=pr,
                    material=item.material,
                    quantity=deficit_qty,
                    unit_price=item.unit_price,
                )
        return pr


class PurchaseRequestItem(models.Model):
    purchase_request = models.ForeignKey(PurchaseRequest, on_delete=models.CASCADE, related_name="items")
    material = models.ForeignKey(Material, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=15, decimal_places=3, validators=[MIN_QTY])
    unit_price = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0.00"), validators=[MIN_PRICE])

    @property
    def line_total(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"))

    class Meta:
        verbose_name = "Позиция заявки"
        verbose_name_plural = "Позиции заявки"


class MonitoringSnapshot(models.Model):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    projects_open = models.IntegerField(default=0)
    projects_done = models.IntegerField(default=0)
    active_tasks = models.IntegerField(default=0)
    overdue_tasks = models.IntegerField(default=0)
    estimates_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0.00"))