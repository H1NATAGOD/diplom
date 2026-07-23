from django.db import models
from django.core.validators import MinValueValidator
from decimal import Decimal

# 1. Справочник типов контрагентов (Поставщик/Заказчик) [cite: 210]
class CounterpartyType(models.Model):
    name = models.CharField(max_length=60, verbose_name="Тип контрагента")

    def __str__(self):
        return self.name

# 2. Контрагенты [cite: 208]
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

    def __str__(self):
        return self.name

# 3. Договоры [cite: 212]
class Contract(models.Model):
    contract_number = models.CharField(max_length=60, verbose_name="Номер договора")
    name = models.CharField(max_length=60, verbose_name="Наименование")
    conclusion_date = models.DateTimeField(verbose_name="Дата заключения")
    status = models.CharField(max_length=60, verbose_name="Статус")
    # Промежуточная связь из таблицы counterparties_and_contracts [cite: 228]
    counterparty = models.ForeignKey(Counterparty, on_delete=models.CASCADE)
    classification = models.ForeignKey(CounterpartyType, on_delete=models.PROTECT)

# 4. Объекты/Проекты (связующее звено) [cite: 146]
class Project(models.Model):
    contract = models.ForeignKey(Contract, on_delete=models.SET_NULL, null=True)
    name = models.CharField(max_length=100, verbose_name="Название объекта")
    address = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=50, default="Заявка")
    start_date = models.DateField(null=True)
    end_date = models.DateField(null=True)

# 5. Сметы [cite: 214]
class Estimate(models.Model):
    project = models.OneToOneField(Project, on_delete=models.CASCADE)
    name = models.CharField(max_length=60)
    date = models.DateTimeField(auto_now_add=True)
    unforeseen_expenses = models.CharField(max_length=60, verbose_name="Непредвиденные расходы %")
    profit = models.DecimalField(max_digits=15, decimal_places=2, verbose_name="Планируемая прибыль")
    total_cost = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)

# 6. Затраты (Прямые и Накладные) [cite: 220, 222]
class CostItem(models.Model):
    COST_TYPES = (('DIRECT', 'Прямые'), ('OVERHEAD', 'Накладные'))
    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name='items')
    type = models.CharField(max_length=10, choices=COST_TYPES)
    service_name = models.CharField(max_length=60)
    price = models.DecimalField(max_digits=15, decimal_places=2)

# 7. Сотрудники и Бригады [cite: 216, 218]
class Worker(models.Model):
    first_name = models.CharField(max_length=60)
    second_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    passport = models.CharField(max_length=60)
    age_date = models.CharField(max_length=60) # В ПЗ указано как varchar [cite: 216]
    inn = models.CharField(max_length=60)
    snils = models.CharField(max_length=60)

class Brigade(models.Model):
    name = models.CharField(max_length=60)
    specific = models.CharField(max_length=60, verbose_name="Специализация")
    workers = models.ManyToManyField(Worker, verbose_name="Состав бригады")

# 8. Задачи для диаграммы Ганта [cite: 127]
class Task(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    brigade = models.ForeignKey(Brigade, on_delete=models.SET_NULL, null=True)
    name = models.CharField(max_length=100)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    progress = models.IntegerField(default=0)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True)