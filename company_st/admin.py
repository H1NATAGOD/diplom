from django.contrib import admin
from .models import (
    CounterpartyType, Counterparty, Contract, Project,
    Estimate, CostItem, Worker, Brigade, Task
)

# Вложенная форма для позиций сметы (Прямые и Накладные расходы)
class CostItemInline(admin.TabularInline):
    model = CostItem
    extra = 1 # Количество пустых строк для новых записей

# Настройка отображения Контрагентов
@admin.register(Counterparty)
class CounterpartyAdmin(admin.ModelAdmin):
    list_display = ('name', 'inn', 'telephone_number', 'email') # Колонки в списке
    search_fields = ('name', 'inn') # Поиск по ФИО или ИНН
    list_filter = ('legal_entities',) # Фильтр по форме собственности

# Настройка отображения Смет
@admin.register(Estimate)
class EstimateAdmin(admin.ModelAdmin):
    list_display = ('name', 'project', 'profit', 'total_cost', 'date')
    inlines = [CostItemInline] # Позволяет наполнять смету затратами на одной странице [cite: 238]
    readonly_fields = ('total_cost',) # Поле расчета только для чтения (вычисляется логикой)

# Настройка отображения Проектов (Воронка объектов)
@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'start_date', 'end_date', 'contract')
    list_editable = ('status',) # Позволяет менять статус прямо в списке [cite: 112]
    list_filter = ('status', 'start_date')

# Настройка отображения Сотрудников
@admin.register(Worker)
class WorkerAdmin(admin.ModelAdmin):
    list_display = ('second_name', 'first_name', 'last_name', 'inn', 'snils')
    search_fields = ('second_name', 'inn', 'snils') [cite: 259]

# Настройка отображения Бригад
@admin.register(Brigade)
class BrigadeAdmin(admin.ModelAdmin):
    list_display = ('name', 'specific')
    filter_horizontal = ('workers',) # Удобный интерфейс выбора сотрудников в бригаду

# Регистрация остальных простых справочников
admin.site.register(CounterpartyType)
admin.site.register(Contract)
admin.site.register(Task)