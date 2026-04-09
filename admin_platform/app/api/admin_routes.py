from fastapi import APIRouter, Request, Depends, Query, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from typing import Optional
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Image, Spacer
from PIL import Image as PILImage
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from io import BytesIO
import sqlite3
import urllib.request
from urllib.parse import quote
import tempfile
import re
import os
import glob
import json
from PyPDF2 import PdfMerger
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font, Alignment, PatternFill, colors
from openpyxl.styles.colors import Color
from openpyxl.writer.excel import save_workbook
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..db.database import get_db
from ..models.schemas import User
from ..services.auth_service import require_role, get_current_user

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

pdfmetrics.registerFont(TTFont('AppleGothic', '/System/Library/Fonts/Supplemental/AppleGothic.ttf'))

@router.get("/settings")
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "manager"], use_cache=False))
):
    settings_result = db.execute(text("SELECT value, start_date, end_date FROM settings WHERE key = 'max_overtime_hours'")).fetchone()
    max_overtime_hours = settings_result[0] if settings_result else 12
    start_date = settings_result[1] if settings_result else None
    end_date = settings_result[2] if settings_result else None

    pdf_approver_result = db.execute(text("SELECT value FROM settings WHERE key = 'pdf_approver'")).fetchone()
    pdf_approver = pdf_approver_result[0] if pdf_approver_result else None

    employees = db.execute(text("SELECT name FROM employees WHERE role IN ('admin', 'manager', 'lead')")).fetchall()

    return templates.TemplateResponse("settings.html", {
        "request": request, 
        "current_user": current_user, 
        "max_overtime_hours": max_overtime_hours, 
        "start_date": start_date, 
        "end_date": end_date,
        "pdf_approver": pdf_approver,
        "employees": employees
    })

@router.post("/settings")
def update_settings(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "manager"], use_cache=False)),
    max_overtime_hours: int = Form(...),
    start_date: str = Form(None),
    end_date: str = Form(None),
    pdf_approver: str = Form(None)
):
    db.execute(text("UPDATE settings SET value = :value, start_date = :start_date, end_date = :end_date WHERE key = 'max_overtime_hours'"), {"value": max_overtime_hours, "start_date": start_date, "end_date": end_date})
    
    # Update or Insert PDF approver setting
    approver_setting = db.execute(text("SELECT 1 FROM settings WHERE key = 'pdf_approver'")).fetchone()
    if approver_setting:
        db.execute(text("UPDATE settings SET value = :value WHERE key = 'pdf_approver'"), {"value": pdf_approver})
    else:
        db.execute(text("INSERT INTO settings (key, value) VALUES ('pdf_approver', :value)"), {"value": pdf_approver})

    db.commit()
    return RedirectResponse(url="/settings", status_code=303)

@router.get("/stats")
def stats_page(
    request: Request,
    current_user: User = Depends(require_role(["admin", "manager", "lead"], use_cache=False))
):
    return templates.TemplateResponse("stats.html", {"request": request, "current_user": current_user})

@router.get("/api/stats/employee-status")
def get_employee_status(db: Session = Depends(get_db), current_user: User = Depends(require_role(["admin", "manager", "lead"], use_cache=False))):
    employees_result = db.execute(text("SELECT name FROM employees WHERE name != 'admin'")).fetchall()
    employees = [row[0] for row in employees_result]

    status_data = []
    for emp_name in employees:
        # Calculate remaining compensatory hours
        overtime_requests = db.execute(text("SELECT content FROM requests WHERE name = :name AND type = '시간외 근무' AND status = 'approved'"), {"name": emp_name}).fetchall()
        total_overtime_hours = 0
        for req in overtime_requests:
            content = json.loads(req[0])
            total_overtime_hours += content.get('calculated_compensatory_hours', 0)

        leave_requests = db.execute(text("SELECT content FROM requests WHERE name = :name AND type = '대휴 사용' AND status = 'approved'"), {"name": emp_name}).fetchall()
        used_leave_hours = 0
        for req in leave_requests:
            content = json.loads(req[0])
            used_leave_hours += content.get('hours', 0)

        remaining_hours = total_overtime_hours - used_leave_hours

        # Calculate remaining development cost
        dev_cost_requests = db.execute(text("SELECT content FROM requests WHERE name = :name AND type = '자기개발비' AND status = 'approved'"), {"name": emp_name}).fetchall()
        used_dev_cost = 0
        for req in dev_cost_requests:
            content = json.loads(req[0])
            used_dev_cost += int(content.get('cost', '0'))
        
        remaining_dev_cost = 2000000 - used_dev_cost

        status_data.append({
            "name": emp_name,
            "remaining_compensatory_hours": remaining_hours,
            "remaining_dev_cost": remaining_dev_cost
        })

    return JSONResponse(content=status_data)

@router.get("/api/stats/overtime-hours")
def get_overtime_hours(
    period: str = Query("monthly", enum=["monthly", "yearly"]),
    month: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "manager", "lead"], use_cache=False))
):
    employees_result = db.execute(text("SELECT name FROM employees WHERE name NOT IN ('admin', 'lead')")).fetchall()
    employees = [row[0] for row in employees_result]

    labels = []
    data = []
    today = datetime.today()

    for emp_name in employees:
        labels.append(emp_name)
        total_hours = 0

        if period == "monthly":
            if month:
                target_month_dt = datetime.strptime(month, "%Y-%m")
            else:
                target_month_dt = today
            
            end_date = (target_month_dt.replace(day=15)).strftime("%Y-%m-%d")
            start_date = (target_month_dt.replace(day=16) - relativedelta(months=1)).strftime("%Y-%m-%d")

        else: # yearly
            start_date = today.replace(month=1, day=1).strftime("%Y-%m-%d")
            end_date = today.replace(month=12, day=31).strftime("%Y-%m-%d")

        requests_result = db.execute(text("SELECT content FROM requests WHERE name = :name AND type = '시간외 근무' AND status = 'approved' AND created BETWEEN :start_date AND :end_date"), {"name": emp_name, "start_date": start_date, "end_date": end_date}).fetchall()
        for req in requests_result:
            content = json.loads(req[0])
            total_hours += content.get('work_hours_weekday', 0) + content.get('work_hours_holiday', 0)
        
        data.append(total_hours)

    return JSONResponse(content={"labels": labels, "data": data})

@router.get("/admin-dashboard")
def admin_dashboard(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    selected_name: Optional[str] = Query(None),
    request_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "manager", "lead"], use_cache=False)),
    overtime_page: int = 1,
    compensatory_page: int = 1,
    trip_page: int = 1,
    dev_page: int = 1,
    per_page: int = 10
):
    # Fetch unique names for the dropdown
    names_result = db.execute(text("SELECT DISTINCT name FROM requests ORDER BY name ASC")).fetchall()
    names = [row[0] for row in names_result]

    # Date filtering logic
    if not start_date or not end_date:
        today = datetime.today()
        if today.day < 16:
            start_of_month = today.replace(day=16) - relativedelta(months=1)
        else:
            start_of_month = today.replace(day=16)
        end_of_month = start_of_month + relativedelta(months=1) - relativedelta(days=1)
        
        start_date = start_of_month.strftime("%Y-%m-%d")
        end_date = end_of_month.strftime("%Y-%m-%d")

    # Base query and params
    base_query = "SELECT * FROM requests WHERE created BETWEEN :start_date AND :end_date"
    params = {"start_date": start_date, "end_date": end_date}

    if selected_name and selected_name != "all":
        base_query += " AND name = :name"
        params["name"] = selected_name

    # Pagination
    overtime_offset = (overtime_page - 1) * per_page
    compensatory_offset = (compensatory_page - 1) * per_page
    trip_offset = (trip_page - 1) * per_page
    dev_offset = (dev_page - 1) * per_page

    # Initialize request lists
    overtime_requests = []
    trip_requests = []
    self_dev_rows = []
    compensatory_leave_requests = []
    total_compensatory = 0
    total_trip = 0
    total_dev = 0
    total_overtime = 0

    if not request_type or request_type == "all" or request_type.startswith("시간외 근무"):
        overtime_query = f"{base_query} AND type = '시간외 근무'"
        if request_type == "시간외 근무 - 수당지급":
            overtime_query += " AND json_extract(content, '$.compensation') = '수당지급'"
        elif request_type == "시간외 근무 - 대체휴가":
            overtime_query += " AND json_extract(content, '$.compensation') = '대체휴가'"
        overtime_query += " ORDER BY id DESC LIMIT :limit OFFSET :offset"
        overtime_requests_raw = db.execute(text(overtime_query), {**params, "limit": per_page, "offset": overtime_offset}).fetchall()
        
        overtime_requests = []
        for r in overtime_requests_raw:
            content = json.loads(r[3])
            r = list(r)
            r.append(content.get('compensation'))
            overtime_requests.append(r)

        count_query = f"SELECT COUNT(*) FROM requests WHERE created BETWEEN :start_date AND :end_date AND type = '시간외 근무'"
        if selected_name and selected_name != "all":
            count_query += " AND name = :name"

        if request_type == "시간외 근무 - 수당지급":
            count_query += " AND json_extract(content, '$.compensation') = '수당지급'"
        elif request_type == "시간외 근무 - 대체휴가":
            count_query += " AND json_extract(content, '$.compensation') = '대체휴가'"
        total_overtime = db.execute(text(count_query), params).scalar_one()

    if not request_type or request_type == "all" or request_type in ["대휴사용", "대휴신청"]:
        compensatory_leave_query = f"{base_query} AND type IN ('대휴 사용', '대휴신청') ORDER BY id DESC LIMIT :limit OFFSET :offset"
        compensatory_leave_requests = db.execute(text(compensatory_leave_query), {**params, "limit": per_page, "offset": compensatory_offset}).fetchall()
        count_query = f"SELECT COUNT(*) FROM requests WHERE created BETWEEN :start_date AND :end_date AND type IN ('대휴 사용', '대휴신청')"
        if selected_name and selected_name != "all":
            count_query += " AND name = :name"
        total_compensatory = db.execute(text(count_query), params).scalar_one()

    if not request_type or request_type == "all" or request_type == "출장":
        trip_query = f"{base_query} AND type = '출장' ORDER BY id DESC LIMIT :limit OFFSET :offset"
        trip_requests = db.execute(text(trip_query), {**params, "limit": per_page, "offset": trip_offset}).fetchall()
        count_query = f"SELECT COUNT(*) FROM requests WHERE created BETWEEN :start_date AND :end_date AND type = '출장'"
        if selected_name and selected_name != "all":
            count_query += " AND name = :name"
        total_trip = db.execute(text(count_query), params).scalar_one()

    if not request_type or request_type == "all" or request_type == "자기개발비":
        dev_query = f"{base_query} AND type = '자기개발비' ORDER BY id DESC LIMIT :limit OFFSET :offset"
        self_dev_rows = db.execute(text(dev_query), {**params, "limit": per_page, "offset": dev_offset}).fetchall()
        count_query = f"SELECT COUNT(*) FROM requests WHERE created BETWEEN :start_date AND :end_date AND type = '자기개발비'"
        if selected_name and selected_name != "all":
            count_query += " AND name = :name"
        total_dev = db.execute(text(count_query), params).scalar_one()

    overtime_total_pages = (total_overtime + per_page - 1) // per_page
    compensatory_total_pages = (total_compensatory + per_page - 1) // per_page
    trip_total_pages = (total_trip + per_page - 1) // per_page
    dev_total_pages = (total_dev + per_page - 1) // per_page

    pending_count = db.execute(text("SELECT count(*) FROM requests WHERE status LIKE '%대기' OR status = '재신청'")).scalar_one()
    approved_count = db.execute(text("SELECT count(*) FROM requests WHERE status = 'approved'")).scalar_one()
    rejected_count = db.execute(text("SELECT count(*) FROM requests WHERE status = 'rejected'")).scalar_one()

    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "overtime_requests": overtime_requests,
            "compensatory_leave_requests": compensatory_leave_requests,
            "trip_requests": trip_requests,
            "dev_requests": self_dev_rows,
            "pending_count": pending_count,
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "current_user": current_user,
            "start_date": start_date,
            "end_date": end_date,
            "names": names,
            "selected_name": selected_name,
            "request_type": request_type,
            "now": datetime.now(),
            "overtime_current_page": overtime_page,
            "overtime_total_pages": overtime_total_pages,
            "compensatory_current_page": compensatory_page,
            "compensatory_total_pages": compensatory_total_pages,
            "trip_current_page": trip_page,
            "trip_total_pages": trip_total_pages,
            "dev_current_page": dev_page,
            "dev_total_pages": dev_total_pages
        }
    )

@router.get("/admin-dashboard/pdf/merge")
def merge_pdfs(ids: str, db: Session = Depends(get_db), current_user: User = Depends(require_role(["admin", "manager", "lead"], use_cache=False))):
    request_ids = [int(id) for id in ids.split(',')]

    request_ids_str = ",".join(map(str, request_ids))
    query = f"""SELECT r.id 
                 FROM requests r JOIN employees e ON r.name = e.name 
                 WHERE r.id IN ({request_ids_str}) 
                 ORDER BY e.emp_no ASC, json_extract(r.content, '$.work_date') ASC"""
    sorted_requests_result = db.execute(text(query)).fetchall()
    sorted_request_ids = [row[0] for row in sorted_requests_result]

    merger = PdfMerger()
    
    pdf_contents = []
    for request_id in sorted_request_ids:
        pdf_content = download_pdf(request_id, db, current_user)
        if pdf_content:
            pdf_contents.append(BytesIO(pdf_content))

    if not pdf_contents:
        return HTMLResponse(content="<script>alert('선택된 항목 중 PDF로 생성할 문서가 없습니다.'); window.history.back();</script>")

    for pdf_content in pdf_contents:
        merger.append(pdf_content)

    output_buffer = BytesIO()
    merger.write(output_buffer)
    merger.close()

    output_buffer.seek(0)
    return HTMLResponse(content=output_buffer.read(), media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=merged-report.pdf"})

@router.get("/admin-dashboard/excel")
def export_to_excel(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    selected_name: Optional[str] = Query(None),
    request_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "manager", "lead"], use_cache=False))
):
    # ... (Excel export logic remains the same)
    pass

def download_pdf(request_id: int, db: Session, current_user: User) -> bytes:
    print(f"--- Generating PDF for request_id: {request_id} ---")
    request_data_result = db.execute(text("SELECT * FROM requests WHERE id = :id"), {"id": request_id}).fetchone()

    if not request_data_result:
        print("!!! Request not found")
        raise HTTPException(status_code=404, detail="Request not found")

    request_data = dict(request_data_result._mapping)
    print(f"Request Data: {request_data}")

    request_content_str = request_data.get('content', '{}')
    request_content = json.loads(request_content_str)

    if request_data.get('type') == '시간외 근무' and request_content.get('compensation') == '대체휴가':
        return None # 대체휴가는 PDF 생성 안함

    # Determine the approver for the PDF
    pdf_approver_setting = db.execute(text("SELECT value FROM settings WHERE key = 'pdf_approver'")).fetchone()
    
    approver_name = None
    if pdf_approver_setting and pdf_approver_setting[0]:
        approver_name = pdf_approver_setting[0]
    else:
        approver_name = request_data.get('approver')

    signature_data = None
    approver_position = "(미지정)"

    if approver_name:
        approver_info_result = db.execute(text("SELECT signature, position FROM employees WHERE name = :name"), {"name": approver_name}).fetchone()
        if approver_info_result:
            approver_info_mapping = dict(approver_info_result._mapping)
            print(f"Approver Info: {approver_info_mapping}")
            signature_data = approver_info_mapping.get('signature')
            approver_position = approver_info_mapping.get('position', '(미지정)')

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    story = []
    styles = getSampleStyleSheet()
    styles['h1'].fontName = 'AppleGothic'
    styles['h1'].alignment = TA_CENTER
    styles['Normal'].fontName = 'AppleGothic'
    styles['Normal'].fontSize = 12

    title = request_data.get('type', '문서')
    story.append(Paragraph(f"{title} 승인 확인서", styles['h1']))
    story.append(Spacer(1, 24))

    request_data_list = [[Paragraph(f"<b>신청자:</b> {request_data['name']}", styles['Normal'])], [Paragraph(f"<b>신청일:</b> {request_data['created'].split(' ')[0]}", styles['Normal'])]]
    key_map = {
        "work_type": "근무 유형", "work_date": "근무일", "work_hours_weekday": "평일 근무시간",
        "work_hours_holiday": "휴일 근무시간", "reason_type": "신청 사유", "reason_detail": "상세 사유",
        "work_location": "근무 장소", "compensation": "보상 유형", "course_title": "수강 항목",
        "purpose": "목적", "course_content": "수강 내용", "cost": "비용", "start_date": "시작일",
        "end_date": "종료일", "reference_site": "참고 사이트", "region": "출장 지역",
        "organization": "출장 기관", "transport": "이동 수단", "hours": "사용 시간"
    }
    for key, value in request_content.items():
        if key in key_map:
            request_data_list.append([Paragraph(f"<b>{key_map[key]}:</b> {value}", styles['Normal'])])

    request_table = Table(request_data_list, hAlign='LEFT')
    request_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'AppleGothic'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
    ]))
    story.append(request_table)
    story.append(Spacer(1, 48))

    story.append(Paragraph("상기 신청을 승인합니다.", styles['Normal']))
    story.append(Spacer(1, 24))

    story.append(Paragraph(f"<b>승인 날짜:</b> {request_data['created'].split(' ')[0]}", styles['Normal']))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"<b>승인자:</b> {approver_position} {approver_name}", styles['Normal']))

    if signature_data:
        print("Signature data found, attempting to process.")
        try:
            img_file = BytesIO(signature_data)
            pil_img = PILImage.open(img_file)
            pil_img.verify()
            img_file.seek(0)
            story.append(Image(img_file, width=50, height=50, hAlign='LEFT'))
            print("Successfully added signature image to story.")
        except Exception as e:
            print(f"!!! Could not process signature image: {e}")
    else:
        print("!!! No signature data found for approver.")

    if not story:
        print("!!! Story is empty, cannot build PDF.")
        return HTMLResponse(content="PDF 생성에 실패했습니다: 내용이 없습니다.", status_code=500)

    print(f"Final story length: {len(story)}")
    doc.build(story)
    buffer.seek(0)
    return buffer.read()

@router.get("/admin-dashboard/pdf/{request_id}")
def download_pdf_route(request_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    request_owner_result = db.execute(text("SELECT name FROM requests WHERE id = :id"), {"id": request_id}).fetchone()
    request_owner = request_owner_result[0] if request_owner_result else None

    if not request_owner or (current_user.name != request_owner and current_user.role not in ["admin", "manager", "lead"]):
        raise HTTPException(status_code=403, detail="이 PDF에 접근할 권한이 없습니다.")
    
    pdf_content = download_pdf(request_id, db, current_user)
    if not pdf_content:
        return HTMLResponse(content="<script>alert('대체휴가 신청은 PDF를 생성하지 않습니다.'); window.history.back();</script>")

    return HTMLResponse(content=pdf_content, media_type="application/pdf")

@router.get("/reject/cancel/{request_id}")
def cancel_rejection(request_id: int, db: Session = Depends(get_db), current_user: User = Depends(require_role(["admin", "manager"]))):
    request_info = db.execute(text("SELECT approver FROM requests WHERE id = :id"), {"id": request_id}).fetchone()
    if not request_info:
        raise HTTPException(status_code=404, detail="Request not found")

    previous_status = f"{dict(request_info._mapping).get('approver', 'manager')} 승인 대기"

    db.execute(text("UPDATE requests SET status = :status, reject_reason = NULL WHERE id = :id"), {
        "status": previous_status,
        "id": request_id
    })
    db.commit()
    return RedirectResponse(url="/approve-list", status_code=303)
