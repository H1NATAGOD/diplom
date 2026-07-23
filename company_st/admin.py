from django.contrib import admin
from .models import (
    CounterpartyType, Counterparty, Contract, Project,
    Estimate, CostItem, Worker, Brigade, Technique, Task,
    InteractionHistory, Material, MaterialPrice, EstimateMaterialItem,
    TaskDependency, TaskResource, Warehouse, WarehouseStock,
    PurchaseRequest, PurchaseRequestItem, MonitoringSnapshot,
    MaterialReservation, StockMovement,
)

# Вложенная форма для позиций сметы (Прямые и Накладные расходы)
class CostItemInline(admin.TabularInline):
    model = CostItem
    extra = 1


class EstimateMaterialItemInline(admin.TabularInline):
    model = EstimateMaterialItem
    extra = 1

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
    inlines = [CostItemInline, EstimateMaterialItemInline]
    readonly_fields = ('total_cost',)

# Настройка отображения Проектов (Воронка объектов)
@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'start_date', 'end_date', 'contract')
    list_editable = ('status',)
    list_filter = ('status', 'start_date')

# Настройка отображения Сотрудников
@admin.register(Worker)
class WorkerAdmin(admin.ModelAdmin):
    list_display = ('second_name', 'first_name', 'last_name', 'inn', 'snils')
    search_fields = ('second_name', 'inn', 'snils')

# Настройка отображения Бригад
@admin.register(Brigade)
class BrigadeAdmin(admin.ModelAdmin):
    list_display = ('name', 'specific')
    filter_horizontal = ('workers',)

# Регистрация остальных простых справочников
admin.site.register(CounterpartyType)
admin.site.register(Contract)
admin.site.register(Technique)
admin.site.register(Task)
admin.site.register(InteractionHistory)
@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ("name", "unit", "min_stock")
    search_fields = ("name",)

admin.site.register(MaterialPrice)
admin.site.register(TaskDependency)
admin.site.register(TaskResource)
admin.site.register(Warehouse)
admin.site.register(WarehouseStock)
admin.site.register(PurchaseRequestItem)
admin.site.register(MonitoringSnapshot)


@admin.register(MaterialReservation)
class MaterialReservationAdmin(admin.ModelAdmin):
    list_display = ("estimate", "warehouse", "material", "quantity", "created_at")
    list_filter = ("warehouse",)


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "warehouse", "material", "movement_type", "quantity", "balance_after", "created_by")
    list_filter = ("movement_type", "warehouse")
    readonly_fields = ("created_at",)


@admin.register(PurchaseRequest)
class PurchaseRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "estimate", "warehouse", "status", "stock_posted", "created_at")
    list_filter = ("status", "stock_posted")