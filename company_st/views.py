from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from io import BytesIO

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from .estimate_export import build_estimate_docx, build_estimate_pdf, build_estimate_txt
from .forms import (
    CostItemForm,
    CreateEstimateForm,
    EstimateMaterialItemForm,
    EstimateMetaForm,
    MaterialForm,
    PurchaseRequestForm,
    PurchaseRequestItemForm,
    WarehouseStockDeltaForm,
    WarehouseStockSetForm,
)
from .models import (
    PurchaseRequestItem,
    Contract,
    CostItem,
    Counterparty,
    Estimate,
    EstimateMaterialItem,
    InteractionHistory,
    Material,
    MaterialPrice,
    MaterialReservation,
    Project,
    PurchaseRequest,
    StockMovement,
    Task,
    TaskDependency,
    Warehouse,
    WarehouseStock,
    Worker,
    Brigade,
    Technique,
)
from .services import build_monitoring_data, create_purchase_request_from_estimate
from .warehouse_service import (
    apply_stock_delta,
    release_estimate_reservations,
    reserve_materials_for_estimate,
    reserved_quantity,
    set_stock_quantity,
    update_purchase_request_status,
)


def _json_ok(data: dict[str, Any], status: int = 200) -> JsonResponse:
    return JsonResponse({"ok": True, "data": data}, status=status)


def _json_error(message: str, status: int = 400, details: Any = None) -> JsonResponse:
    return JsonResponse({"ok": False, "error": {"message": message, "details": details}}, status=status)


def _has_any_group(user, groups: tuple[str, ...]) -> bool:
    return user.is_superuser or user.groups.filter(name__in=groups).exists()


def api_roles_required(*groups: str):
    def decorator(view_func):
        def wrapped(request: HttpRequest, *args, **kwargs):
            if not request.user.is_authenticated:
                return _json_error("Требуется авторизация", status=401)
            if not _has_any_group(request.user, tuple(groups)):
                return _json_error("Недостаточно прав", status=403)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


def _paginate_qs(request: HttpRequest, qs):
    page = max(1, int(request.GET.get("page", "1")))
    page_size = min(100, max(1, int(request.GET.get("page_size", "20"))))
    total = qs.count()
    start = (page - 1) * page_size
    return qs[start : start + page_size], {"page": page, "page_size": page_size, "total": total}


def _json_body(request: HttpRequest) -> dict[str, Any]:
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def home(request: HttpRequest):
    """Главная страница: без входа — на логин, после входа — на диспетчер ролей."""
    if request.user.is_authenticated:
        return redirect("dispatch_user")
    return redirect("login")



class UserLoginView(LoginView):
    template_name = "company_st/login.html"

def is_manager(user):
    return user.is_superuser or user.groups.filter(name='Менеджер отдела продаж').exists()

def is_finance(user):
    return user.is_superuser or user.groups.filter(name='Финансист').exists()

def is_site_manager(user):
    return user.is_superuser or user.groups.filter(name='Начальник участка').exists()

@login_required
def dispatch_user(request):
    if request.user.is_superuser or is_manager(request.user):
        return redirect('manager_dashboard')
    elif is_finance(request.user):
        return redirect('finance_dashboard')
    elif is_site_manager(request.user):
        return redirect('site_manager_dashboard')
    return redirect('admin:index')

def _can_access_projects(user):
    return user.is_superuser or is_manager(user) or is_site_manager(user) or is_finance(user)


def _can_hr(user):
    return user.is_superuser or is_site_manager(user) or is_manager(user)


def _can_warehouse(user):
    return user.is_superuser or is_finance(user) or is_site_manager(user)



@login_required
@user_passes_test(is_manager)
def manager_dashboard(request):
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Укажите название объекта.")
        else:
            cid = request.POST.get("contract_id")
            contract_id = int(cid) if cid else None
            p = Project.objects.create(
                name=name,
                address=(request.POST.get("address") or "").strip() or None,
                status=request.POST.get("status") or "LEAD",
                start_date=request.POST.get("start_date") or None,
                end_date=request.POST.get("end_date") or None,
                contract_id=contract_id,
            )
            if request.POST.get("create_estimate"):
                est = Estimate.objects.create(
                    project=p,
                    name=f"Смета — {p.name}",
                    unforeseen_expenses="0",
                    profit=Decimal("0"),
                )
                est.recalc_total()
            messages.success(request, "Объект создан.")
        return redirect("manager_dashboard")

    projects = Project.objects.select_related("contract", "contract__counterparty").order_by("-id")
    contracts = Contract.objects.select_related("counterparty").order_by("-id")[:200]
    return render(
        request,
        "company_st/manager_dashboard.html",
        {"projects": projects, "contracts": contracts, "status_choices": Project.PIPELINE_STATUSES},
    )


@login_required
@user_passes_test(is_finance)
@require_POST
def finance_quick_material(request: HttpRequest):
    return redirect("material_add")


@login_required
@user_passes_test(is_finance)
@require_POST
def finance_update_purchase_status(request: HttpRequest, pk: int):
    purchase_request = get_object_or_404(PurchaseRequest, pk=pk)
    new_status = request.POST.get("status", "")
    allowed_statuses = {choice[0] for choice in PurchaseRequest.STATUS_CHOICES}
    if new_status not in allowed_statuses:
        messages.error(request, "Недопустимый статус заявки.")
        return redirect("finance_dashboard")
    try:
        update_purchase_request_status(purchase_request, new_status, user=request.user)
    except ValidationError as exc:
        messages.error(request, exc.messages[0] if getattr(exc, "messages", None) else str(exc))
        return redirect("finance_dashboard")
    msg = f"Статус заявки #{purchase_request.id} обновлён."
    if new_status == "RECEIVED" and purchase_request.stock_posted:
        msg += " Материалы оприходованы на склад."
    messages.success(request, msg)
    return redirect("finance_dashboard")


@login_required
@user_passes_test(is_finance)
def finance_dashboard(request):
    estimates = Estimate.objects.select_related("project").order_by("-id")
    purchase_requests = PurchaseRequest.objects.select_related("estimate", "warehouse", "estimate__project").order_by("-id")[:50]
    return render(
        request,
        "company_st/finance_dashboard.html",
        {
            "estimates": estimates,
            "purchase_requests": purchase_requests,
            "purchase_status_choices": PurchaseRequest.STATUS_CHOICES,
        },
    )


@login_required
@user_passes_test(_can_warehouse)
def warehouse_list(request):
    warehouses = Warehouse.objects.order_by("name")
    return render(request, "company_st/warehouse_list.html", {"warehouses": warehouses})


@login_required
@user_passes_test(_can_warehouse)
def warehouse_add(request):
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Укажите название склада.")
        else:
            Warehouse.objects.create(
                name=name,
                address=(request.POST.get("address") or "").strip(),
            )
            messages.success(request, "Склад создан.")
            return redirect("warehouse_list")
    return render(request, "company_st/warehouse_form.html", {"title": "Новый склад"})


@login_required
@user_passes_test(_can_warehouse)
def warehouse_edit(request, pk: int):
    warehouse = get_object_or_404(Warehouse, pk=pk)
    if request.method == "POST":
        warehouse.name = (request.POST.get("name") or warehouse.name).strip()
        warehouse.address = (request.POST.get("address") or "").strip()
        warehouse.save()
        messages.success(request, "Склад обновлён.")
        return redirect("warehouse_list")
    return render(request, "company_st/warehouse_form.html", {"title": "Редактирование склада", "warehouse": warehouse})


@login_required
@user_passes_test(_can_warehouse)
def warehouse_detail(request, pk: int):
    warehouse = get_object_or_404(Warehouse, pk=pk)
    materials = Material.objects.order_by("name")
    stocks = WarehouseStock.objects.select_related("material").filter(warehouse=warehouse).order_by("material__name")

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "set_stock":
                form = WarehouseStockSetForm(request.POST)
                if not form.is_valid():
                    raise ValidationError(list(form.errors.values())[0][0])
                material = get_object_or_404(Material, pk=form.cleaned_data["material_id"])
                set_stock_quantity(warehouse, material, form.cleaned_data["quantity"], user=request.user)
                messages.success(request, "Остаток сохранён.")
            elif action == "move_stock":
                form = WarehouseStockDeltaForm(request.POST)
                if not form.is_valid():
                    raise ValidationError(list(form.errors.values())[0][0])
                material = get_object_or_404(Material, pk=form.cleaned_data["material_id"])
                apply_stock_delta(
                    warehouse,
                    material,
                    form.cleaned_data["delta"],
                    user=request.user,
                    comment=form.cleaned_data.get("comment") or "",
                )
                messages.success(request, "Операция выполнена.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0] if getattr(exc, "messages", None) else str(exc))
        return redirect("warehouse_detail", pk=warehouse.pk)

    movements = (
        StockMovement.objects.select_related("material", "created_by", "estimate", "purchase_request")
        .filter(warehouse=warehouse)[:100]
    )
    stock_rows = []
    for s in stocks:
        reserved = reserved_quantity(warehouse, s.material)
        stock_rows.append(
            {
                "stock": s,
                "reserved": reserved,
                "available": s.quantity - reserved,
            }
        )

    return render(
        request,
        "company_st/warehouse_detail.html",
        {
            "warehouse": warehouse,
            "materials": materials,
            "stocks": stocks,
            "stock_rows": stock_rows,
            "movements": movements,
            "movement_types": StockMovement.MovementType,
        },
    )


@login_required
@user_passes_test(_can_warehouse)
def material_list(request):
    q = (request.GET.get("q") or "").strip()
    materials = Material.objects.order_by("name")
    if q:
        materials = materials.filter(name__icontains=q)
    return render(request, "company_st/material_list.html", {"materials": materials, "q": q})


@login_required
@user_passes_test(_can_warehouse)
def material_add(request):
    form = MaterialForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Материал добавлен в справочник.")
        return redirect("material_list")
    return render(request, "company_st/material_form.html", {"form": form, "title": "Новый материал"})


@login_required
@user_passes_test(_can_warehouse)
def material_edit(request, pk: int):
    material = get_object_or_404(Material, pk=pk)
    form = MaterialForm(request.POST or None, instance=material)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Материал обновлён.")
        return redirect("material_list")
    return render(request, "company_st/material_form.html", {"form": form, "title": "Редактирование материала", "material": material})


@login_required
@user_passes_test(lambda u: u.is_superuser or is_finance(u))
@require_POST
def material_delete(request, pk: int):
    material = get_object_or_404(Material, pk=pk)
    if WarehouseStock.objects.filter(material=material).exists() or EstimateMaterialItem.objects.filter(material=material).exists():
        messages.error(request, "Нельзя удалить: материал используется в остатках или сметах.")
    else:
        material.delete()
        messages.success(request, "Материал удалён.")
    return redirect("material_list")


@login_required
@user_passes_test(is_finance)
def purchase_request_list(request):
    status = request.GET.get("status", "")
    qs = PurchaseRequest.objects.select_related("estimate", "warehouse", "estimate__project").order_by("-id")
    if status:
        qs = qs.filter(status=status)
    return render(
        request,
        "company_st/purchase_request_list.html",
        {"purchase_requests": qs[:100], "status_filter": status, "status_choices": PurchaseRequest.STATUS_CHOICES},
    )


@login_required
@user_passes_test(is_finance)
def purchase_request_create(request):
    if request.method == "POST":
        form = PurchaseRequestForm(request.POST)
        if form.is_valid():
            pr = form.save(commit=False)
            pr.save()
            messages.success(request, f"Заявка #{pr.id} создана. Добавьте позиции.")
            return redirect("purchase_request_edit", pk=pr.pk)
    else:
        form = PurchaseRequestForm(initial={"status": "DRAFT"})
    return render(request, "company_st/purchase_request_form.html", {"form": form, "title": "Новая заявка на закупку"})


@login_required
@user_passes_test(is_finance)
def purchase_request_edit(request, pk: int):
    purchase_request = get_object_or_404(
        PurchaseRequest.objects.select_related("estimate", "warehouse", "estimate__project"),
        pk=pk,
    )
    locked = purchase_request.stock_posted
    header_form = PurchaseRequestForm(request.POST or None, instance=purchase_request)

    if request.method == "POST":
        action = request.POST.get("action", "save_header")
        if action == "save_header":
            if locked:
                messages.error(request, "Заявка оприходована — изменяйте только через склад.")
            elif header_form.is_valid():
                old_status = purchase_request.status
                pr = header_form.save(commit=False)
                pr.save()
                if pr.status == "RECEIVED" and old_status != "RECEIVED":
                    try:
                        update_purchase_request_status(pr, "RECEIVED", user=request.user)
                    except ValidationError as exc:
                        messages.error(request, exc.messages[0] if getattr(exc, "messages", None) else str(exc))
                        return redirect("purchase_request_edit", pk=pk)
                messages.success(request, "Заявка сохранена.")
            else:
                messages.error(request, "Исправьте ошибки в форме заявки.")
        elif action == "add_item" and not locked:
            item_form = PurchaseRequestItemForm(request.POST)
            if item_form.is_valid():
                item = item_form.save(commit=False)
                item.purchase_request = purchase_request
                item.save()
                messages.success(request, "Позиция добавлена.")
            else:
                for errs in item_form.errors.values():
                    messages.error(request, errs[0])
        elif action == "delete_item" and not locked:
            item = get_object_or_404(PurchaseRequestItem, pk=request.POST.get("item_id"), purchase_request=purchase_request)
            item.delete()
            messages.info(request, "Позиция удалена.")
        return redirect("purchase_request_edit", pk=pk)

    items = purchase_request.items.select_related("material")
    item_form = PurchaseRequestItemForm()
    return render(
        request,
        "company_st/purchase_request_edit.html",
        {
            "purchase_request": purchase_request,
            "header_form": header_form,
            "item_form": item_form,
            "items": items,
            "locked": locked,
            "materials": Material.objects.order_by("name"),
        },
    )


@login_required
@user_passes_test(lambda u: u.is_superuser or is_finance(u) or is_manager(u))
def create_estimate(request, project_id: int):
    project = get_object_or_404(Project, pk=project_id)
    form = CreateEstimateForm(request.POST or None, project=project)
    if request.method == "POST" and form.is_valid():
        estimate = form.save(commit=False)
        estimate.project = project
        estimate.save()
        estimate.recalc_total()
        messages.success(request, "Смета создана.")
        if is_finance(request.user) or request.user.is_superuser:
            return redirect("estimate_detail", pk=estimate.pk)
        return redirect("project_detail", pk=project.pk)
    return render(
        request,
        "company_st/create_estimate.html",
        {"form": form, "project": project},
    )


@login_required
@user_passes_test(is_site_manager)
def site_manager_dashboard(request):
    brigades = Brigade.objects.prefetch_related("workers").order_by("name")
    workers = Worker.objects.order_by("last_name", "first_name")
    techniques = Technique.objects.select_related("worker", "brigade").order_by("name")
    projects = Project.objects.order_by("name")[:100]
    return render(
        request,
        "company_st/site_dashboard.html",
        {
            "brigades": brigades,
            "workers": workers,
            "techniques": techniques,
            "projects": projects,
        },
    )



@login_required
@user_passes_test(_can_access_projects)
def project_list(request):
    projects = Project.objects.select_related("contract__counterparty").order_by("-id")
    return render(request, "company_st/projects.html", {"projects": projects})


@login_required
@user_passes_test(_can_access_projects)
def project_detail(request, pk):
    project = get_object_or_404(Project.objects.select_related("contract__counterparty"), pk=pk)
    tasks = project.tasks.select_related("responsible_brigade").order_by("start_date")
    interactions = project.interactions.order_by("-created_at")[:50]
    estimates = project.estimates.order_by("-date", "-id")
    if request.method == "POST" and is_manager(request.user):
        InteractionHistory.objects.create(
            project=project,
            interaction_type=request.POST.get("interaction_type", "CALL"),
            note=request.POST.get("note", ""),
        )
        messages.success(request, "Взаимодействие записано.")
        return redirect("project_detail", pk=pk)
    return render(
        request,
        "company_st/project_detail.html",
        {
            "project": project,
            "tasks": tasks,
            "interactions": interactions,
            "estimates": estimates,
            "brigades": Brigade.objects.order_by("name"),
            "interaction_types": InteractionHistory.INTERACTION_TYPES,
            "can_manage_crm": request.user.is_superuser or is_manager(request.user),
            "can_finance": request.user.is_superuser or is_finance(request.user),
        },
    )


@login_required
@user_passes_test(is_manager)
def add_project(request):
    contracts = Contract.objects.select_related("counterparty").order_by("-id")[:200]
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Укажите название.")
        else:
            cid = request.POST.get("contract_id")
            contract_id = int(cid) if cid else None
            p = Project.objects.create(
                name=name,
                address=(request.POST.get("address") or "").strip() or None,
                status=request.POST.get("status") or "LEAD",
                start_date=request.POST.get("start_date") or None,
                end_date=request.POST.get("end_date") or None,
                contract_id=contract_id,
            )
            if request.POST.get("create_estimate"):
                est = Estimate.objects.create(
                    project=p,
                    name=f"Смета — {p.name}",
                    unforeseen_expenses=(request.POST.get("unforeseen_expenses") or "0").strip(),
                    profit=Decimal(request.POST.get("profit") or "0"),
                )
                est.recalc_total()
            messages.success(request, "Объект создан.")
            return redirect("manager_dashboard")
    return render(
        request,
        "company_st/add_project.html",
        {"contracts": contracts, "status_choices": Project.PIPELINE_STATUSES},
    )


@login_required
@user_passes_test(is_manager)
def edit_project(request, pk):
    project = get_object_or_404(Project, pk=pk)
    contracts = Contract.objects.select_related("counterparty").order_by("-id")[:200]
    if request.method == "POST":
        project.name = (request.POST.get("name") or project.name).strip()
        project.address = (request.POST.get("address") or "").strip() or None
        project.status = request.POST.get("status") or project.status
        project.start_date = request.POST.get("start_date") or None
        project.end_date = request.POST.get("end_date") or None
        cid = request.POST.get("contract_id")
        project.contract_id = int(cid) if cid else None
        project.save()
        messages.success(request, "Объект сохранён.")
        return redirect("project_detail", pk=project.pk)
    return render(
        request,
        "company_st/edit_project.html",
        {"project": project, "contracts": contracts, "status_choices": Project.PIPELINE_STATUSES},
    )



@login_required
@user_passes_test(_can_hr)
def worker_list(request):
    workers = Worker.objects.order_by("last_name", "first_name")
    return render(request, "company_st/workers.html", {"workers": workers})


@login_required
@user_passes_test(_can_hr)
def add_worker(request):
    if request.method == "POST":
        Worker.objects.create(
            last_name=request.POST.get("last_name", ""),
            first_name=request.POST.get("first_name", ""),
            second_name=request.POST.get("second_name", ""),
            passport=request.POST.get("passport", ""),
            inn=request.POST.get("inn", ""),
            snils=request.POST.get("snils", ""),
            age_date=request.POST.get("age_date", ""),
        )
        messages.success(request, "Сотрудник добавлен.")
        return redirect("worker_list")
    return render(request, "company_st/add_worker.html")


@login_required
@user_passes_test(_can_hr)
def edit_worker(request, pk):
    worker = get_object_or_404(Worker, pk=pk)
    if request.method == "POST":
        worker.first_name = request.POST.get("first_name", "")
        worker.second_name = request.POST.get("second_name", "")
        worker.last_name = request.POST.get("last_name", "")
        worker.passport = request.POST.get("passport", "")
        worker.inn = request.POST.get("inn", "")
        worker.snils = request.POST.get("snils", "")
        worker.age_date = request.POST.get("age_date", "")
        worker.save()
        messages.success(request, "Данные сохранены.")
        return redirect("worker_list")
    return render(request, "company_st/edit_worker.html", {"worker": worker})


@login_required
@user_passes_test(_can_hr)
@require_POST
def delete_worker(request, pk):
    worker = get_object_or_404(Worker, pk=pk)
    worker.delete()
    messages.info(request, "Сотрудник удалён.")
    return redirect("worker_list")



@login_required
@user_passes_test(lambda u: u.is_superuser or is_site_manager(u))
def brigade_add(request):
    workers = Worker.objects.order_by("last_name", "first_name")
    if request.method == "POST":
        b = Brigade.objects.create(
            name=request.POST.get("name", "Бригада"),
            specific=request.POST.get("specific", ""),
        )
        ids = request.POST.getlist("worker_ids")
        if ids:
            b.workers.set(Worker.objects.filter(pk__in=ids))
        messages.success(request, "Бригада создана.")
        return redirect("site_manager_dashboard")
    return render(request, "company_st/brigade_form.html", {"workers": workers, "title": "Новая бригада"})


@login_required
@user_passes_test(lambda u: u.is_superuser or is_site_manager(u))
def brigade_edit(request, pk):
    brigade = get_object_or_404(Brigade.objects.prefetch_related("workers"), pk=pk)
    workers = Worker.objects.order_by("last_name", "first_name")
    if request.method == "POST":
        brigade.name = request.POST.get("name", brigade.name)
        brigade.specific = request.POST.get("specific", "")
        brigade.save()
        ids = request.POST.getlist("worker_ids")
        brigade.workers.set(Worker.objects.filter(pk__in=ids))
        messages.success(request, "Бригада обновлена.")
        return redirect("site_manager_dashboard")
    return render(
        request,
        "company_st/brigade_form.html",
        {"workers": workers, "brigade": brigade, "title": "Редактирование бригады"},
    )



@login_required
@user_passes_test(lambda u: u.is_superuser or is_site_manager(u))
def technique_list(request):
    items = Technique.objects.select_related("worker", "brigade").order_by("name")
    return render(request, "company_st/technique.html", {"items": items})


@login_required
@user_passes_test(lambda u: u.is_superuser or is_site_manager(u))
def add_technique(request):
    workers = Worker.objects.order_by("last_name", "first_name")
    brigades = Brigade.objects.order_by("name")
    if request.method == "POST":
        wid = request.POST.get("worker")
        bid = request.POST.get("brigade")
        Technique.objects.create(
            name=request.POST.get("name", ""),
            worker_id=int(wid) if wid else None,
            brigade_id=int(bid) if bid else None,
            end_date=request.POST.get("end_date") or None,
        )
        messages.success(request, "Техника добавлена.")
        return redirect("technique_list")
    return render(request, "company_st/add_technique.html", {"workers": workers, "brigades": brigades})


@login_required
@user_passes_test(lambda u: u.is_superuser or is_site_manager(u))
def edit_technique(request, pk):
    item = get_object_or_404(Technique, pk=pk)
    workers = Worker.objects.order_by("last_name", "first_name")
    brigades = Brigade.objects.order_by("name")
    if request.method == "POST":
        item.name = request.POST.get("name", "")
        wid = request.POST.get("worker")
        bid = request.POST.get("brigade")
        item.worker_id = int(wid) if wid else None
        item.brigade_id = int(bid) if bid else None
        item.end_date = request.POST.get("end_date") or None
        item.save()
        messages.success(request, "Сохранено.")
        return redirect("technique_list")
    return render(request, "company_st/edit_technique.html", {"item": item, "workers": workers, "brigades": brigades})


@login_required
@user_passes_test(lambda u: u.is_superuser or is_site_manager(u) or is_manager(u))
def add_task(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    brigades = Brigade.objects.order_by("name")
    if request.method == "POST":
        Task.objects.create(
            project=project,
            name=request.POST.get("name", ""),
            description=request.POST.get("description", ""),
            start_date=request.POST.get("start_date"),
            end_date=request.POST.get("end_date"),
            status=request.POST.get("status", "TODO"),
            responsible_brigade_id=int(request.POST.get("responsible_brigade")) if request.POST.get("responsible_brigade") else None,
        )
        messages.success(request, "Задача добавлена.")
        return redirect("project_detail", pk=project_id)
    return render(request, "company_st/add_task.html", {"project": project, "brigades": brigades})


@login_required
@user_passes_test(lambda u: u.is_superuser or is_site_manager(u) or is_manager(u))
def update_task_status(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if request.method == "POST":
        task.status = request.POST.get("status", task.status)
        task.save()
        messages.success(request, "Статус обновлён.")
        return redirect("project_detail", pk=task.project.pk)
    return redirect("project_list")



@login_required
@user_passes_test(is_finance)
def estimate_detail(request, pk):
    estimate = get_object_or_404(
        Estimate.objects.select_related("project"),
        pk=pk,
    )
    materials = Material.objects.order_by("name")
    suppliers = Counterparty.objects.order_by("name")[:300]

    meta_form = EstimateMetaForm(instance=estimate)
    cost_form = CostItemForm()
    material_form = EstimateMaterialItemForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_cost":
            cost_form = CostItemForm(request.POST)
            if cost_form.is_valid():
                item = cost_form.save(commit=False)
                item.estimate = estimate
                item.save()
                messages.success(request, "Строка добавлена.")
            else:
                for errs in cost_form.errors.values():
                    messages.error(request, errs[0])
        elif action == "delete_cost":
            get_object_or_404(CostItem, pk=request.POST.get("cost_id"), estimate=estimate).delete()
            messages.info(request, "Строка удалена.")
        elif action == "add_material":
            material_form = EstimateMaterialItemForm(request.POST)
            if material_form.is_valid():
                item = material_form.save(commit=False)
                item.estimate = estimate
                item.save()
                messages.success(request, "Материал добавлен.")
            else:
                for errs in material_form.errors.values():
                    messages.error(request, errs[0])
        elif action == "delete_material":
            get_object_or_404(EstimateMaterialItem, pk=request.POST.get("mat_id"), estimate=estimate).delete()
            messages.info(request, "Позиция удалена.")
        elif action == "save_meta":
            meta_form = EstimateMetaForm(request.POST, instance=estimate)
            if meta_form.is_valid():
                meta_form.save()
                estimate.recalc_total()
                messages.success(request, "Смета сохранена и пересчитана.")
            else:
                for errs in meta_form.errors.values():
                    messages.error(request, errs[0])
        elif action == "add_price":
            mid = request.POST.get("price_material_id")
            sid = request.POST.get("supplier_id")
            MaterialPrice.objects.create(
                material_id=int(mid),
                supplier_id=int(sid) if sid else None,
                price=Decimal(request.POST.get("price_value") or "0"),
                is_active=True,
            )
            messages.success(request, "Цена добавлена в справочник.")
        elif action == "purchase":
            wid = request.POST.get("warehouse_id")
            wh = get_object_or_404(Warehouse, pk=wid) if wid else None
            create_purchase_request_from_estimate(estimate=estimate, warehouse=wh)
            messages.success(request, "Заявка на закупку создана из сметы.")
        elif action == "reserve":
            wid = request.POST.get("warehouse_id")
            if not wid:
                messages.error(request, "Выберите склад для резервирования.")
            else:
                wh = get_object_or_404(Warehouse, pk=wid)
                created = reserve_materials_for_estimate(estimate, wh, user=request.user)
                if created:
                    messages.success(request, f"Зарезервировано позиций: {len(created)}.")
                else:
                    messages.warning(request, "Нечего резервировать или недостаточно свободного остатка.")
        elif action == "release_reserve":
            wid = request.POST.get("warehouse_id")
            if not wid:
                messages.error(request, "Выберите склад.")
            else:
                wh = get_object_or_404(Warehouse, pk=wid)
                count = release_estimate_reservations(estimate, wh, user=request.user)
                if count:
                    messages.success(request, f"Снято резервов: {count}.")
                else:
                    messages.info(request, "Активных резервов по этой смете нет.")
        return redirect("estimate_detail", pk=estimate.pk)

    estimate.refresh_from_db()
    reservations = (
        MaterialReservation.objects.select_related("warehouse", "material")
        .filter(estimate=estimate, quantity__gt=0)
        .order_by("warehouse__name", "material__name")
    )
    if request.method != "POST":
        meta_form = EstimateMetaForm(instance=estimate)

    return render(
        request,
        "company_st/estimate_detail.html",
        {
            "estimate": estimate,
            "meta_form": meta_form,
            "cost_form": cost_form,
            "material_form": material_form,
            "cost_items": estimate.items.all(),
            "material_lines": estimate.material_items.select_related("material"),
            "materials": materials,
            "suppliers": suppliers,
            "prices": MaterialPrice.objects.select_related("material", "supplier").filter(is_active=True).order_by("-valid_from")[:100],
            "warehouses": Warehouse.objects.order_by("name"),
            "reservations": reservations,
        },
    )


@require_GET
@api_roles_required("Менеджер отдела продаж", "Финансист", "Начальник участка")
def api_funnel(request: HttpRequest) -> JsonResponse:
    payload = Project.objects.values("status").order_by("status").annotate(count=Count("id"))
    return _json_ok({"funnel": list(payload)})


@require_POST
@api_roles_required("Менеджер отдела продаж")
def api_add_interaction(request: HttpRequest, project_id: int) -> JsonResponse:
    project = get_object_or_404(Project, pk=project_id)
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
        interaction = InteractionHistory.objects.create(
            project=project,
            interaction_type=data.get("interaction_type", "CALL"),
            note=data.get("note", ""),
        )
        return _json_ok({"interaction_id": interaction.id}, status=201)
    except ValidationError as exc:
        return _json_error("Ошибка валидации", 400, exc.message_dict if hasattr(exc, "message_dict") else str(exc))


@require_POST
@api_roles_required("Финансист")
def api_recalc_estimate(request: HttpRequest, estimate_id: int) -> JsonResponse:
    estimate = get_object_or_404(Estimate, pk=estimate_id)
    total = estimate.recalc_total()
    return _json_ok({"estimate_id": estimate.id, "total_cost": str(total)})


@require_GET
@api_roles_required("Финансист", "Начальник участка")
def api_material_prices(request: HttpRequest) -> JsonResponse:
    material_id = request.GET.get("material_id")
    qs = MaterialPrice.objects.select_related("material", "supplier").filter(is_active=True)
    if material_id:
        qs = qs.filter(material_id=material_id)
    data = [
        {
            "id": p.id,
            "material_id": p.material_id,
            "material": p.material.name,
            "supplier": p.supplier.name if p.supplier else None,
            "price": str(p.price),
            "valid_from": p.valid_from.isoformat(),
        }
        for p in qs[:500]
    ]
    return _json_ok({"items": data})


@require_GET
@api_roles_required("Начальник участка", "Менеджер отдела продаж")
def api_gantt(request: HttpRequest) -> JsonResponse:
    tasks = Task.objects.select_related("project").all()
    deps = TaskDependency.objects.all()
    return _json_ok(
        {
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "project": t.project.name,
                    "start_date": t.start_date.isoformat(),
                    "end_date": t.end_date.isoformat(),
                    "status": t.status,
                    "progress": t.progress,
                }
                for t in tasks
            ],
            "dependencies": [{"task_id": d.task_id, "depends_on_id": d.depends_on_id} for d in deps],
        }
    )


@require_POST
@api_roles_required("Финансист", "Начальник участка")
def api_create_purchase_request(request: HttpRequest, estimate_id: int) -> JsonResponse:
    estimate = get_object_or_404(Estimate, pk=estimate_id)
    data = json.loads(request.body.decode("utf-8") or "{}")
    warehouse = None
    warehouse_id = data.get("warehouse_id")
    if warehouse_id:
        warehouse = get_object_or_404(Warehouse, pk=warehouse_id)
    purchase = create_purchase_request_from_estimate(estimate=estimate, warehouse=warehouse)
    return _json_ok(
        {
            "purchase_request_id": purchase.id,
            "status": purchase.status,
            "items": [
                {
                    "material_id": i.material_id,
                    "material": i.material.name,
                    "quantity": str(i.quantity),
                    "unit_price": str(i.unit_price),
                }
                for i in purchase.items.select_related("material")
            ],
        },
        status=201,
    )


@require_GET
@api_roles_required("Менеджер отдела продаж", "Финансист", "Начальник участка")
def api_monitoring_summary(request: HttpRequest) -> JsonResponse:
    summary = build_monitoring_data()
    return _json_ok(
        {
            "projects_open": summary.projects_open,
            "projects_done": summary.projects_done,
            "active_tasks": summary.active_tasks,
            "overdue_tasks": summary.overdue_tasks,
            "estimates_total": str(summary.estimates_total),
        }
    )


@require_GET
@api_roles_required("Финансист")
def api_generate_estimate_document(request: HttpRequest, estimate_id: int) -> HttpResponse:
    estimate = get_object_or_404(Estimate.objects.select_related("project"), pk=estimate_id)
    estimate.recalc_total()
    body = build_estimate_txt(estimate)
    response = HttpResponse(body, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="smeta_{estimate.id}.txt"'
    return response


@require_GET
@api_roles_required("Финансист")
def api_export_estimate_pdf(request: HttpRequest, estimate_id: int) -> HttpResponse:
    estimate = get_object_or_404(Estimate.objects.select_related("project"), pk=estimate_id)
    estimate.recalc_total()
    response = HttpResponse(build_estimate_pdf(estimate), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="smeta_{estimate.id}.pdf"'
    return response


@require_GET
@api_roles_required("Финансист")
def api_export_estimate_docx(request: HttpRequest, estimate_id: int) -> HttpResponse:
    estimate = get_object_or_404(Estimate.objects.select_related("project"), pk=estimate_id)
    estimate.recalc_total()
    response = HttpResponse(
        build_estimate_docx(estimate),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="smeta_{estimate.id}.docx"'
    return response


@login_required
@user_passes_test(is_finance)
@require_GET
def export_estimate_pdf(request: HttpRequest, pk: int) -> HttpResponse:
    estimate = get_object_or_404(Estimate.objects.select_related("project"), pk=pk)
    estimate.recalc_total()
    response = HttpResponse(build_estimate_pdf(estimate), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="smeta_{estimate.id}.pdf"'
    return response


@login_required
@user_passes_test(is_finance)
@require_GET
def export_estimate_docx(request: HttpRequest, pk: int) -> HttpResponse:
    estimate = get_object_or_404(Estimate.objects.select_related("project"), pk=pk)
    estimate.recalc_total()
    response = HttpResponse(
        build_estimate_docx(estimate),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="smeta_{estimate.id}.docx"'
    return response


@require_GET
@api_roles_required("Менеджер отдела продаж", "Финансист", "Начальник участка")
def api_projects(request: HttpRequest) -> JsonResponse:
    qs = Project.objects.all().order_by("-id")
    status = request.GET.get("status")
    q = request.GET.get("q")
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(name__icontains=q)
    page_items, page_meta = _paginate_qs(request, qs)
    data = [
        {"id": p.id, "name": p.name, "status": p.status, "start_date": p.start_date, "end_date": p.end_date}
        for p in page_items
    ]
    return _json_ok({"items": data, "pagination": page_meta})


@require_POST
@api_roles_required("Менеджер отдела продаж")
def api_projects_create(request: HttpRequest) -> JsonResponse:
    data = _json_body(request)
    obj = Project.objects.create(
        name=data.get("name", ""),
        status=data.get("status", "LEAD"),
        address=data.get("address", ""),
        start_date=data.get("start_date") or None,
        end_date=data.get("end_date") or None,
        contract_id=data.get("contract_id") or None,
    )
    return _json_ok({"id": obj.id}, status=201)


@require_POST
@api_roles_required("Менеджер отдела продаж")
def api_projects_update(request: HttpRequest, project_id: int) -> JsonResponse:
    obj = get_object_or_404(Project, pk=project_id)
    data = _json_body(request)
    for field in ("name", "status", "address", "start_date", "end_date", "contract_id"):
        if field in data:
            setattr(obj, field, data[field] or None)
    obj.save()
    return _json_ok({"id": obj.id})


@require_POST
@api_roles_required("Менеджер отдела продаж")
def api_projects_delete(request: HttpRequest, project_id: int) -> JsonResponse:
    obj = get_object_or_404(Project, pk=project_id)
    obj.delete()
    return _json_ok({"deleted": True})


@require_GET
@api_roles_required("Финансист", "Менеджер отдела продаж")
def api_estimates(request: HttpRequest) -> JsonResponse:
    qs = Estimate.objects.select_related("project").order_by("-id")
    project_id = request.GET.get("project_id")
    if project_id:
        qs = qs.filter(project_id=project_id)
    page_items, page_meta = _paginate_qs(request, qs)
    data = [{"id": e.id, "name": e.name, "project": e.project.name, "total_cost": str(e.total_cost or 0)} for e in page_items]
    return _json_ok({"items": data, "pagination": page_meta})


@require_GET
@api_roles_required("Финансист", "Начальник участка")
def api_materials(request: HttpRequest) -> JsonResponse:
    qs = Material.objects.all().order_by("name")
    q = request.GET.get("q")
    if q:
        qs = qs.filter(name__icontains=q)
    page_items, page_meta = _paginate_qs(request, qs)
    data = [{"id": m.id, "name": m.name, "unit": m.unit} for m in page_items]
    return _json_ok({"items": data, "pagination": page_meta})


@require_POST
@api_roles_required("Финансист")
def api_materials_create(request: HttpRequest) -> JsonResponse:
    data = _json_body(request)
    obj = Material.objects.create(name=data.get("name", ""), unit=data.get("unit", "шт"))
    return _json_ok({"id": obj.id}, status=201)


@require_POST
@api_roles_required("Финансист")
def api_materials_update(request: HttpRequest, material_id: int) -> JsonResponse:
    obj = get_object_or_404(Material, pk=material_id)
    data = _json_body(request)
    if "name" in data:
        obj.name = data["name"]
    if "unit" in data:
        obj.unit = data["unit"]
    obj.save()
    return _json_ok({"id": obj.id})


@require_POST
@api_roles_required("Финансист")
def api_materials_delete(request: HttpRequest, material_id: int) -> JsonResponse:
    obj = get_object_or_404(Material, pk=material_id)
    obj.delete()
    return _json_ok({"deleted": True})


@require_GET
@api_roles_required("Начальник участка", "Менеджер отдела продаж")
def api_tasks(request: HttpRequest) -> JsonResponse:
    qs = Task.objects.select_related("project").order_by("-id")
    project_id = request.GET.get("project_id")
    status = request.GET.get("status")
    if project_id:
        qs = qs.filter(project_id=project_id)
    if status:
        qs = qs.filter(status=status)
    page_items, page_meta = _paginate_qs(request, qs)
    data = [
        {"id": t.id, "project_id": t.project_id, "name": t.name, "status": t.status, "start_date": t.start_date, "end_date": t.end_date}
        for t in page_items
    ]
    return _json_ok({"items": data, "pagination": page_meta})


@require_GET
@api_roles_required("Финансист", "Начальник участка")
def api_warehouses(request: HttpRequest) -> JsonResponse:
    qs = Warehouse.objects.all().order_by("name")
    q = request.GET.get("q")
    if q:
        qs = qs.filter(name__icontains=q)
    page_items, page_meta = _paginate_qs(request, qs)
    data = [{"id": w.id, "name": w.name, "address": w.address} for w in page_items]
    return _json_ok({"items": data, "pagination": page_meta})


@require_GET
@api_roles_required("Финансист", "Начальник участка")
def api_purchase_requests(request: HttpRequest) -> JsonResponse:
    qs = PurchaseRequest.objects.select_related("estimate", "warehouse").order_by("-id")
    status = request.GET.get("status")
    if status:
        qs = qs.filter(status=status)
    page_items, page_meta = _paginate_qs(request, qs)
    data = [
        {
            "id": p.id,
            "estimate_id": p.estimate_id,
            "warehouse_id": p.warehouse_id,
            "status": p.status,
            "created_at": p.created_at.isoformat(),
        }
        for p in page_items
    ]
    return _json_ok({"items": data, "pagination": page_meta})