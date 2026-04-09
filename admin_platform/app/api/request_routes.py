import calendar
import json
import re
import shutil
import os
from fastapi import APIRouter, Request, Form, Depends, UploadFile, File
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..db.database import get_db, get_approver_by_role, get_approver_email
from ..models.schemas import User
from ..services.email_service import send_email
from ..services.auth_service import get_current_user, require_role

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
def save_upload_file(upload_file: UploadFile, destination: str) -> str:
    try:
        with open(destination, "wb") as buffer:
            shutil.copyfileobj(upload_file.file, buffer)
    finally:
        upload_file.file.close()
    return destination

# --- Pages ---
@router.get("/request/compensatory-leave", response_class=HTMLResponse)
def compensatory_leave_form(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user, use_cache=False)):
    # 사용자의 총 대휴 획득 시간 계산
    total_compensatory_hours_result = db.execute(text("SELECT SUM(CAST(json_extract(content, '$.calculated_compensatory_hours') AS INTEGER)) FROM requests WHERE name = :name AND type = '시간외 근무' AND status = 'approved'"), {"name": current_user.name}).fetchone()
    total_compensatory_hours = total_compensatory_hours_result[0] or 0

    # 사용자가 사용한 대휴 시간 계산
    used_compensatory_hours_result = db.execute(text("SELECT SUM(CAST(json_extract(content, '$.hours') AS INTEGER)) FROM requests WHERE name = :name AND type = '대휴신청' AND status = 'approved'"), {"name": current_user.name}).fetchone()
    used_compensatory_hours = used_compensatory_hours_result[0] or 0

    remaining_hours = total_compensatory_hours - used_compensatory_hours

    return templates.TemplateResponse("compensatory_leave_form.html", {"request": request, "remaining_hours": remaining_hours, "current_user": current_user})

@router.get("/request/overtime", response_class=HTMLResponse)
def overtime_page(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user, use_cache=False)):
    max_overtime_hours_result = db.execute(text("SELECT value FROM settings WHERE key = 'max_overtime_hours'")).fetchone()
    max_overtime_hours = int(max_overtime_hours_result[0]) if max_overtime_hours_result else 12

    today = date.today()
    start_of_month = today.replace(day=1)
    _, last_day = calendar.monthrange(today.year, today.month)
    end_of_month = today.replace(day=last_day)

    used_overtime_result = db.execute(text("SELECT SUM(CAST(json_extract(content, '$.calculated_approved_hours') AS INTEGER)) FROM requests WHERE name = :name AND type = '시간외 근무' AND status = 'approved' AND work_date BETWEEN :start_date AND :end_date"), {"name": current_user.name, "start_date": start_of_month, "end_date": end_of_month}).fetchone()
    used_overtime = used_overtime_result[0] or 0

    remaining_overtime = max_overtime_hours - used_overtime

    return templates.TemplateResponse("overtime_form.html", {"request": request, "current_user": current_user, "remaining_overtime": remaining_overtime, "max_overtime_hours": max_overtime_hours})

@router.get("/request/business-trip", response_class=HTMLResponse)
def business_trip_form(request: Request, current_user: User = Depends(get_current_user, use_cache=False)):
    return templates.TemplateResponse("business_trip_form.html", {"request": request, "current_user": current_user})

@router.get("/request/self-development", response_class=HTMLResponse)
def self_development_form(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user, use_cache=False)):
    # 자기개발비 잔액 (연간 200만원 한도)
    current_year = datetime.now().strftime('%Y')
    used_dev_cost_result = db.execute(text("SELECT SUM(cost) FROM requests WHERE name = :name AND type = '자기개발비' AND strftime('%Y', created) = :year AND status = 'approved'"), {"name": current_user.name, "year": current_year}).fetchone()
    used_dev_cost = used_dev_cost_result[0] or 0
    remaining_dev_cost = 2000000 - used_dev_cost

    return templates.TemplateResponse("self_development_form.html", {"request": request, "current_user": current_user, "remaining_dev_cost": remaining_dev_cost})

# --- Form Submissions ---
@router.post("/request/compensatory-leave")
def handle_compensatory_leave(request: Request, leave_date: date = Form(...), hours: int = Form(...), reason: str = Form(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user, use_cache=False)):
    # 사용자의 총 대휴 획득 시간 계산
    total_compensatory_hours_result = db.execute(text("SELECT SUM(CAST(json_extract(content, '$.calculated_compensatory_hours') AS INTEGER)) FROM requests WHERE name = :name AND type = '시간외 근무' AND status = 'approved'"), {"name": current_user.name}).fetchone()
    total_compensatory_hours = total_compensatory_hours_result[0] or 0

    # 사용자가 사용한 대휴 시간 계산
    used_compensatory_hours_result = db.execute(text("SELECT SUM(CAST(json_extract(content, '$.hours') AS INTEGER)) FROM requests WHERE name = :name AND type = '대휴신청' AND status = 'approved'"), {"name": current_user.name}).fetchone()
    used_compensatory_hours = used_compensatory_hours_result[0] or 0

    remaining_hours = total_compensatory_hours - used_compensatory_hours

    if hours > remaining_hours:
        return HTMLResponse(content=f"<script>alert('잔여 대휴 시간({remaining_hours}시간)을 초과하여 신청할 수 없습니다.'); window.location.href = '/request/compensatory-leave';</script>")

    content = json.dumps({"leave_date": str(leave_date), "hours": hours, "reason": reason})
    approver_role = 'manager'
    db.execute(text("INSERT INTO requests (name, type, content, status, approver, created) VALUES (:name, :type, :content, :status, :approver, :created)"), {
        "name": current_user.name, 
        "type": '대휴신청', 
        "content": content, 
        "status": f'{approver_role} 승인 대기', 
        "approver": approver_role,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    db.commit()

    return RedirectResponse(url="/dashboard", status_code=303)

@router.post("/request/overtime")
async def save_overtime(
    req: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user, use_cache=False),
    work_type: str = Form(...),
    work_date: str = Form(...),
    work_hours_weekday: str = Form('0'),
    work_hours_holiday: str = Form('0'),
    reason_type: str = Form(...),
    reason_detail: str = Form(...),
    work_location: str = Form(...),
    compensation: str = Form(...)
):
    form_data = await req.form()
    work_time_range = form_data.get("work_time_range")
    work_hours_weekday = int(work_hours_weekday) if work_hours_weekday else 0
    work_hours_holiday_val = int(work_hours_holiday or '0')

    if work_type == "평일연장근로":
        total_hours = work_hours_weekday
        work_hours_holiday_val = 0
    else:
        total_hours = work_hours_holiday_val
    
    calculated_approved_hours = 0
    calculated_compensatory_hours = 0

    if compensation == "수당지급":
        max_overtime_hours_result = db.execute(text("SELECT value FROM settings WHERE key = 'max_overtime_hours'")).fetchone()
        max_overtime_hours = int(max_overtime_hours_result[0]) if max_overtime_hours_result else 12

        today = date.today()
        start_of_month = today.replace(day=1)
        _, last_day = calendar.monthrange(today.year, today.month)
        end_of_month = today.replace(day=last_day)

        used_overtime_result = db.execute(text("SELECT SUM(CAST(json_extract(content, '$.calculated_approved_hours') AS INTEGER)) FROM requests WHERE name = :name AND type = '시간외 근무' AND status = 'approved' AND work_date BETWEEN :start_date AND :end_date"), {"name": current_user.name, "start_date": start_of_month, "end_date": end_of_month}).fetchone()
        used_overtime = used_overtime_result[0] or 0

        if used_overtime + total_hours > max_overtime_hours:
            return HTMLResponse(content=f"<script>alert('월 시간외 근무 한도({max_overtime_hours}시간)를 초과합니다. 잔여 시간: {max_overtime_hours - used_overtime}시간'); window.location.href = '/request/overtime';</script>")

        calculated_approved_hours = total_hours
    elif compensation == "대체휴가":
        calculated_compensatory_hours = total_hours

    content_data = {
        "work_type": work_type,
        "work_date": work_date,
        "work_hours_weekday": work_hours_weekday,
        "work_hours_holiday": work_hours_holiday_val,
        "reason_type": reason_type,
        "reason_detail": reason_detail,
        "work_location": work_location,
        "work_time_range": work_time_range,
        "compensation": compensation,
        "calculated_approved_hours": calculated_approved_hours,
        "calculated_compensatory_hours": calculated_compensatory_hours
    }

    approver_role = 'manager'
    db.execute(text("INSERT INTO requests (name, type, status, created, approver, content) VALUES (:name, :type, :status, :created, :approver, :content)"), {
        "name": current_user.name, 
        "type": "시간외 근무", 
        "status": f"{approver_role} 승인 대기", 
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
        "approver": approver_role, 
        "content": json.dumps(content_data, ensure_ascii=False)
    })
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)

@router.post("/request/business-trip")
def submit_business_trip(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user, use_cache=False),
    start_date: str = Form(...),
    end_date: str = Form(None),
    region: str = Form(...),
    region_other: str = Form(None),
    organization: str = Form(...),
    purpose: str = Form(...),
    purpose_other: str = Form(None),
    transport: str = Form(...)
):
    content_data = {
        "start_date": start_date,
        "end_date": end_date,
        "region": region,
        "region_other": region_other,
        "organization": organization,
        "purpose": purpose,
        "purpose_other": purpose_other,
        "transport": transport
    }
    approver_role = 'manager'
    db.execute(text("INSERT INTO requests (name, type, status, created, approver, content) VALUES (:name, :type, :status, :created, :approver, :content)"), {
        "name": current_user.name, 
        "type": "출장", 
        "status": f"{approver_role} 승인 대기", 
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
        "approver": approver_role, 
        "content": json.dumps(content_data, ensure_ascii=False)
    })
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)

@router.post("/request/self-development")
def submit_self_development(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user, use_cache=False),
    course_title: str = Form(...),
    purpose: str = Form(...),
    course_content: str = Form(...),
    cost: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(None),
    reference_site: str = Form(None),
    file: UploadFile = File(None)
):
    file_path = None
    if file and file.filename:
        file_path = save_upload_file(file, f"documents/{file.filename}")

    content_data = {
        "course_title": course_title,
        "purpose": purpose,
        "course_content": course_content,
        "cost": cost,
        "start_date": start_date,
        "end_date": end_date,
        "reference_site": reference_site
    }
    approver_role = 'manager'
    db.execute(text("INSERT INTO requests (name, type, status, created, approver, cost, content, file_path) VALUES (:name, :type, :status, :created, :approver, :cost, :content, :file_path)"), {
        "name": current_user.name, 
        "type": "자기개발비", 
        "status": f"{approver_role} 승인 대기", 
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
        "approver": approver_role, 
        "cost": int(cost), 
        "content": json.dumps(content_data, ensure_ascii=False), 
        "file_path": file_path
    })
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)

# --- APIs ---
@router.post("/request/overtime/calculate")
def calculate_overtime(db: Session = Depends(get_db), current_user: User = Depends(get_current_user, use_cache=False), total_hours: int = Form(...)):
    max_overtime_hours_result = db.execute(text("SELECT value FROM settings WHERE key = 'max_overtime_hours'")).fetchone()
    max_overtime_hours = int(max_overtime_hours_result[0]) if max_overtime_hours_result else 12

    today = date.today()
    start_of_month = today.replace(day=1)
    _, last_day = calendar.monthrange(today.year, today.month)
    end_of_month = today.replace(day=last_day)

    used_overtime_result = db.execute(text("SELECT SUM(CAST(json_extract(content, '$.calculated_approved_hours') AS INTEGER)) FROM requests WHERE name = :name AND type = '시간외 근무' AND status = 'approved' AND work_date BETWEEN :start_date AND :end_date"), {"name": current_user.name, "start_date": start_of_month, "end_date": end_of_month}).fetchone()
    used_overtime = used_overtime_result[0] or 0

    remaining_allowance = max_overtime_hours - used_overtime
    approved_hours = min(total_hours, remaining_allowance)
    compensatory_hours = total_hours - approved_hours

    return JSONResponse(content={
        "monthly_approved_hours": used_overtime,
        "remaining_allowance": remaining_allowance,
        "approved_hours": approved_hours,
        "compensatory_hours": compensatory_hours
    })

# --- Admin/Manager Routes ---
@router.get("/approve-list")
def approve_list(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_role(["admin", "manager", "lead"])), start_date: str = None, end_date: str = None, selected_name: str = None, request_type: str = None, page: int = 1, status_filter: str = None):
    print(f"request_type: {request_type}, status_filter: {status_filter}")
    # Fetch unique names for the dropdown
    names_result = db.execute(text("SELECT DISTINCT name FROM requests ORDER BY name ASC")).fetchall()
    names = [row[0] for row in names_result]

    if not start_date or not end_date:
        today = date.today()
        if today.day < 16:
            start_of_month = today.replace(day=16) - relativedelta(months=1)
        else:
            start_of_month = today.replace(day=16)
        end_of_month = start_of_month + relativedelta(months=1) - relativedelta(days=1)
        
        start_date = start_of_month.strftime("%Y-%m-%d")
        end_date = end_of_month.strftime("%Y-%m-%d")

    if not start_date or not end_date:
        today = date.today()
        if today.day < 16:
            start_of_month = today.replace(day=16) - relativedelta(months=1)
        else:
            start_of_month = today.replace(day=16)
        end_of_month = start_of_month + relativedelta(months=1) - relativedelta(days=1)
        
        start_date = start_of_month.strftime("%Y-%m-%d")
        end_date = end_of_month.strftime("%Y-%m-%d")

    end_date_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    end_date_str = end_date_dt.strftime("%Y-%m-%d")
    query = "SELECT id, name, type, content, status, created, approver, reject_reason, cost, approved_by_lead, approved_by_manager, approved_by_lead_at, approved_by_manager_at, file_path FROM requests WHERE created >= :start_date AND created < :end_date"
    params = {"start_date": start_date, "end_date": end_date_str}
    params = {"start_date": start_date, "end_date": end_date_str}

    if selected_name and selected_name != "all":
        query += " AND name = :name"
        params["name"] = selected_name

    if request_type:
        request_type = request_type.strip()
    print(f"Filtered request_type: {request_type}") # 디버깅 코드 추가

    if request_type and request_type != "all":
        if request_type == "시간외 근무-수당지급":
            query += " AND type = '시간외 근무' AND json_extract(content, '$.compensation') = '수당지급'"
        elif request_type == "시간외 근무-대체휴가":
            query += " AND type = '시간외 근무' AND json_extract(content, '$.compensation') = '대체휴가'"
        elif request_type == "시간외 근무-대체휴가":
            query += " AND type IN ('시간외 근무', '오버타임') AND json_extract(content, '$.compensation') = '수당지급'"
        elif request_type == "시간외 근무-대체휴가":
            query += " AND type IN ('시간외 근무', '오버타임') AND json_extract(content, '$.compensation') = '대체휴가'"
        elif request_type == "대휴신청":
            query += " AND type = '대휴신청'"
        else:
            query += " AND type = :type"
            params["type"] = request_type

    if status_filter:
        if status_filter == 'pending':
            query += " AND status LIKE '%대기'"
        else:
            query += " AND status = :status"
            params["status"] = status_filter

    query += " ORDER BY id DESC"

    all_requests = db.execute(text(query), params).fetchall()
    print(f"all_requests: {all_requests}")
    
    total_requests = len(all_requests)
    requests_per_page = 10
    total_pages = (total_requests + requests_per_page - 1) // requests_per_page
    
    start_index = (page - 1) * requests_per_page
    end_index = start_index + requests_per_page
    paginated_requests = []
    for row in all_requests[start_index:end_index]:
        row_dict = dict(row._mapping)
        try:
            content = json.loads(row_dict['content'])
            row_dict['content'] = content
        except (json.JSONDecodeError, TypeError):
            pass  # content가 JSON이 아니거나 None인 경우 무시

        if row_dict['type'] == '출장':
            start_date_dt = None
            end_date_dt = None
            if row_dict['content'] and row_dict['content'].get('start_date'):
                start_date_dt = datetime.strptime(row_dict['content']['start_date'], '%Y-%m-%d')
            if row_dict['content'] and row_dict['content'].get('end_date'):
                end_date_dt = datetime.strptime(row_dict['content']['end_date'], '%Y-%m-%d')
            row_dict['start_date_dt'] = start_date_dt
            row_dict['end_date_dt'] = end_date_dt
        


        paginated_requests.append(row_dict)

    # 통계 데이터 계산
    pending_count_result = db.execute(text("SELECT count(*) FROM requests WHERE status LIKE '%대기'")).fetchone()
    pending_count = pending_count_result[0]

    approved_count_result = db.execute(text("SELECT count(*) FROM requests WHERE status = 'approved'")).fetchone()
    approved_count = approved_count_result[0]

    rejected_count_result = db.execute(text("SELECT count(*) FROM requests WHERE status = 'rejected'")).fetchone()
    rejected_count = rejected_count_result[0]

    total_count_result = db.execute(text("SELECT count(*) FROM requests")).fetchone()
    total_count = total_count_result[0]

    return templates.TemplateResponse("approve_list.html", {
        "request": request, 
        "requests": paginated_requests, 
        "current_user": current_user, 
        "start_date": start_date, 
        "end_date": end_date, 
        "names": names,
        "selected_name": selected_name,
        "request_type": request_type,
        "current_page": page,
        "total_pages": total_pages,
        "pending_count": pending_count,
        "approved_count": approved_count,
        "rejected_count": rejected_count,
        "total_count": total_count
    })

@router.get("/approve")
def approve_request(id: int, db: Session = Depends(get_db), current_user: User = Depends(require_role(["admin", "manager", "lead"]))):
    # Check if the current user has permission to approve
    current_approver_result = db.execute(text("SELECT approver FROM requests WHERE id = :id"), {"id": id}).fetchone()
    current_approver = current_approver_result[0]

    if current_user.role not in ['admin', 'manager', 'lead']:
        return RedirectResponse(url="/approve-list", status_code=303)

    # Final approval
    if current_user.role == 'admin':
        db.execute(text("UPDATE requests SET status = :status, approver = :approver, approved_by_manager = :approved_by_manager, approved_by_manager_at = :approved_by_manager_at WHERE id = :id"), {
            "status": "approved", 
            "approver": current_user.name, 
            "approved_by_manager": current_user.name, 
            "approved_by_manager_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 
            "id": id
        })
    elif current_user.role == 'manager':
        db.execute(text("UPDATE requests SET status = :status, approver = :approver, approved_by_manager = :approved_by_manager, approved_by_manager_at = :approved_by_manager_at WHERE id = :id"), {
            "status": "approved", 
            "approver": current_user.name, 
            "approved_by_manager": current_user.name, 
            "approved_by_manager_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 
            "id": id
        })
    else: # lead
        db.execute(text("UPDATE requests SET status = :status, approver = :approver, approved_by_lead = :approved_by_lead, approved_by_lead_at = :approved_by_lead_at WHERE id = :id"), {
            "status": "approved", 
            "approver": current_user.name, 
            "approved_by_lead": current_user.name, 
            "approved_by_lead_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 
            "id": id
        })

    db.commit()

    return RedirectResponse(url="/approve-list", status_code=303)

@router.get("/reject")
def reject_form(request: Request, id: int, current_user: User = Depends(require_role(["admin", "manager", "lead"]))):
    return templates.TemplateResponse("reject_form.html", {"request": request, "id": id, "current_user": current_user})

@router.post("/reject")
def reject_submit(id: int = Form(...), reason: str = Form(...), db: Session = Depends(get_db), current_user: User = Depends(require_role(["admin", "manager", "lead"]))):
    db.execute(text("UPDATE requests SET status = 'rejected', reject_reason = :reason WHERE id = :id"), {"reason": reason, "id": id})
    db.commit()
    return RedirectResponse(url="/approve-list", status_code=303)

@router.get("/request/delete/{request_id}")
def delete_request(request_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user, use_cache=False)):
    db.execute(text("DELETE FROM requests WHERE id = :id AND name = :name"), {"id": request_id, "name": current_user.name})
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)

@router.get("/request/delete/admin/{request_id}")
def admin_delete_request(request_id: int, db: Session = Depends(get_db), current_user: User = Depends(require_role(["admin", "manager", "lead"]))):
    db.execute(text("DELETE FROM requests WHERE id = :id"), {"id": request_id})
    db.commit()
    return RedirectResponse(url="/approve-list", status_code=303)

@router.get("/download/{request_id}")
def download_file(request_id: int, db: Session = Depends(get_db), current_user: User = Depends(require_role(["admin", "manager", "lead"]))):
    file_path_result = db.execute(text("SELECT file_path FROM requests WHERE id = :id"), {"id": request_id}).fetchone()
    file_path = file_path_result[0]

    if file_path:
        return FileResponse(path=file_path, filename=os.path.basename(file_path))
    else:
        return HTMLResponse(content="파일을 찾을 수 없습니다.", status_code=404)

@router.get("/request/edit/{request_id}")
def edit_request_form(request_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user, use_cache=False)):
    request_data_result = db.execute(text("SELECT * FROM requests WHERE id = :id AND name = :name"), {"id": request_id, "name": current_user.name}).fetchone()

    if not request_data_result:
        return HTMLResponse(content="신청 내역을 찾을 수 없거나 수정할 권한이 없습니다.", status_code=404)

    request_data = dict(request_data_result._mapping)
    request_data['content'] = json.loads(request_data['content'])

    template_map = {
        "시간외 근무": "overtime_form_edit.html",
        "대휴신청": "compensatory_leave_form.html",
        "출장": "business_trip_form_edit.html",
        "자기개발비": "self_development_form_edit.html",
    }

    template_name = template_map.get(request_data['type'])

    if not template_name:
        return HTMLResponse(content="알 수 없는 신청 종류입니다.", status_code=400)

    if request_data['type'] == '시간외 근무':
        if request_data['content'].get('work_hours_weekday') and int(request_data['content']['work_hours_weekday']) > 0:
            request_data['content']['work_type'] = '평일연장근로'
            request_data['content']['work_hours_holiday'] = 0
        elif request_data['content'].get('work_hours_holiday') and int(request_data['content']['work_hours_holiday']) > 0:
            request_data['content']['work_type'] = '휴일근로'

    print(request_data)
    return templates.TemplateResponse(template_name, {"request": request, "request_data": request_data, "current_user": current_user})

@router.post("/request/edit/overtime/{request_id}")
async def update_overtime_request(
    request_id: int,
    req: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user, use_cache=False),
    work_type: str = Form(...),
    work_date: str = Form(...),
    work_hours_weekday: str = Form('0'),
    work_hours_holiday: str = Form('0'),
    reason_type: str = Form(...),
    reason_detail: str = Form(...),
    work_location: str = Form(...),
    compensation: str = Form(...)
):
    form_data = await req.form()
    work_time_range = form_data.get("work_time_range")
    work_hours_weekday = int(work_hours_weekday) if work_hours_weekday else 0
    work_hours_holiday_val = int(work_hours_holiday or '0')

    if work_type == "평일연장근로":
        total_hours = work_hours_weekday
        work_hours_holiday_val = 0
    else:
        total_hours = work_hours_holiday_val
    
    calculated_approved_hours = 0
    calculated_compensatory_hours = 0

    if compensation == "수당지급":
        calculated_approved_hours = total_hours
    elif compensation == "대체휴가":
        calculated_compensatory_hours = total_hours

    content_data = {
        "work_type": work_type,
        "work_date": work_date,
        "work_hours_weekday": work_hours_weekday,
        "work_hours_holiday": work_hours_holiday_val,
        "reason_type": reason_type,
        "reason_detail": reason_detail,
        "work_location": work_location,
        "work_time_range": work_time_range,
        "compensation": compensation,
        "calculated_approved_hours": calculated_approved_hours,
        "calculated_compensatory_hours": calculated_compensatory_hours
    }

    request_to_update = db.execute(text("SELECT status FROM requests WHERE id = :id"), {"id": request_id}).fetchone()

    update_fields = {
        "content": json.dumps(content_data, ensure_ascii=False),
        "id": request_id,
        "name": current_user.name
    }

    if request_to_update and request_to_update[0] == 'rejected':
        update_fields["status"] = '재신청'
        db.execute(text("UPDATE requests SET content = :content, status = :status WHERE id = :id AND name = :name"), update_fields)
    else:
        db.execute(text("UPDATE requests SET content = :content WHERE id = :id AND name = :name"), update_fields)

    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)

@router.post("/request/edit/business-trip/{request_id}")
def update_business_trip_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user, use_cache=False),
    start_date: str = Form(...),
    end_date: str = Form(None),
    region: str = Form(...),
    region_other: str = Form(None),
    organization: str = Form(...),
    purpose: str = Form(...),
    purpose_other: str = Form(None),
    transport: str = Form(...)
):
    content_data = {
        "start_date": start_date,
        "end_date": end_date,
        "region": region,
        "region_other": region_other,
        "organization": organization,
        "purpose": purpose,
        "purpose_other": purpose_other,
        "transport": transport
    }

    request_to_update = db.execute(text("SELECT status FROM requests WHERE id = :id"), {"id": request_id}).fetchone()

    update_fields = {
        "content": json.dumps(content_data, ensure_ascii=False),
        "id": request_id,
        "name": current_user.name
    }

    if request_to_update and request_to_update[0] == 'rejected':
        update_fields["status"] = '재신청'
        db.execute(text("UPDATE requests SET content = :content, status = :status WHERE id = :id AND name = :name"), update_fields)
    else:
        db.execute(text("UPDATE requests SET content = :content WHERE id = :id AND name = :name"), update_fields)

    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


