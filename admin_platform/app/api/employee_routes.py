from fastapi import APIRouter, Request, Form, Depends, UploadFile, File
import shutil
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text

from ..db.database import get_db
from ..models.schemas import User
from ..services.auth_service import require_role, get_current_user, get_password_hash

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

@router.get("/employee/list")
def employee_list(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_role(["admin", "lead"]))):
    employees = db.execute(text("SELECT * FROM employees")).fetchall()
    return templates.TemplateResponse("employee_list.html", {"request": request, "employees": employees, "current_user": current_user})

@router.get("/employee/manage")
def employee_manage_form(request: Request, current_user: User = Depends(require_role(["admin", "lead"]))):
    return templates.TemplateResponse("employee_manage.html", {"request": request, "current_user": current_user})

@router.post("/employee/manage")
def handle_employee_manage(name: str = Form(...), emp_no: str = Form(...), dept: str = Form(...), position: str = Form(...), work_type: str = Form(...), role: str = Form(...), email: str = Form(...), signature: UploadFile = File(None), db: Session = Depends(get_db), current_user: User = Depends(require_role(["admin", "lead"]))):
    signature_path = None
    if signature and signature.filename:
        signature_path = f"app/static/signatures/{signature.filename}"
        with open(signature_path, "wb") as buffer:
            shutil.copyfileobj(signature.file, buffer)

    hashed_password = get_password_hash("12345")
    db.execute(text("INSERT INTO employees (name, emp_no, dept, position, work_type, role, email, signature, hashed_password) VALUES (:name, :emp_no, :dept, :position, :work_type, :role, :email, :signature, :hashed_password)"), {"name": name, "emp_no": emp_no, "dept": dept, "position": position, "work_type": work_type, "role": role, "email": email, "signature": signature.filename if signature else None, "hashed_password": hashed_password})
    db.commit()
    return RedirectResponse(url="/employee/list", status_code=303)

@router.get("/employee/edit")
def employee_edit_form(request: Request, id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user, use_cache=False)):
    employee = db.execute(text("SELECT * FROM employees WHERE id = :id"), {"id": id}).fetchone()
    return templates.TemplateResponse("employee_edit.html", {"request": request, "employee": employee, "current_user": current_user})

@router.post("/employee/edit")
def handle_employee_edit(id: int = Form(...), name: str = Form(...), emp_no: str = Form(...), dept: str = Form(...), position: str = Form(...), work_type: str = Form(...), role: str = Form(...), email: str = Form(...), signature: UploadFile = File(None), db: Session = Depends(get_db), current_user: User = Depends(require_role(["admin", "lead"]))):
    update_fields = {
        "id": id,
        "name": name,
        "emp_no": emp_no,
        "dept": dept,
        "position": position,
        "work_type": work_type,
        "role": role,
        "email": email
    }
    
    set_clauses = "name = :name, emp_no = :emp_no, dept = :dept, position = :position, work_type = :work_type, role = :role, email = :email"

    if signature and signature.filename:
        signature_data = signature.file.read()
        update_fields['signature'] = signature_data
        set_clauses += ", signature = :signature"

    db.execute(text(f"UPDATE employees SET {set_clauses} WHERE id = :id"), update_fields)
    db.commit()
    return RedirectResponse(url="/employee/list", status_code=303)

@router.post("/employee/delete/{id}")
def employee_delete(id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user, use_cache=False)):
    db.execute(text("DELETE FROM employees WHERE id = :id"), {"id": id})
    db.commit()
    return RedirectResponse(url="/employee/list", status_code=303)
