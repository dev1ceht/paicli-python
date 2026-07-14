"""
学生信息管理系统
功能：增、删、改、查
"""

import json
import os

DATA_FILE = os.path.join(os.path.dirname(__file__), "students.json")


def load_data():
    """从 JSON 文件加载学生数据"""
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def save_data(students):
    """将学生数据保存到 JSON 文件"""
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(students, f, ensure_ascii=False, indent=2)


# ── 增 ──────────────────────────────────────────────

def add_student():
    """添加学生"""
    students = load_data()
    sid = input("请输入学号: ").strip()
    if any(s["id"] == sid for s in students):
        print(f"错误：学号 {sid} 已存在！")
        return
    name = input("请输入姓名: ").strip()
    if not name:
        print("姓名不能为空！")
        return
    try:
        age = int(input("请输入年龄: "))
    except ValueError:
        print("年龄必须是数字！")
        return
    grade = input("请输入成绩: ").strip()
    students.append({"id": sid, "name": name, "age": age, "grade": grade})
    save_data(students)
    print(f"学生 {name} 添加成功！")


# ── 删 ──────────────────────────────────────────────

def delete_student():
    """删除学生"""
    students = load_data()
    if not students:
        print("当前没有学生记录。")
        return
    sid = input("请输入要删除的学号: ").strip()
    for i, s in enumerate(students):
        if s["id"] == sid:
            confirm = input(f"确定删除学生 {s['name']}（学号 {sid}）？(y/n): ").strip().lower()
            if confirm == "y":
                removed = students.pop(i)
                save_data(students)
                print(f"学生 {removed['name']} 已删除！")
            else:
                print("已取消删除。")
            return
    print(f"未找到学号为 {sid} 的学生。")


# ── 改 ──────────────────────────────────────────────

def update_student():
    """修改学生信息"""
    students = load_data()
    if not students:
        print("当前没有学生记录。")
        return
    sid = input("请输入要修改的学号: ").strip()
    for s in students:
        if s["id"] == sid:
            print(f"当前信息：{s}")
            s["name"] = input(f"请输入新姓名（回车保持 [{s['name']}]）: ").strip() or s["name"]
            try:
                new_age = input(f"请输入新年龄（回车保持 [{s['age']}]）: ").strip()
                if new_age:
                    s["age"] = int(new_age)
            except ValueError:
                print("年龄输入无效，保持原值。")
            s["grade"] = input(f"请输入新成绩（回车保持 [{s['grade']}]）: ").strip() or s["grade"]
            save_data(students)
            print(f"学生 {sid} 信息已更新！")
            return
    print(f"未找到学号为 {sid} 的学生。")


# ── 查 ──────────────────────────────────────────────

def list_students():
    """列出所有学生"""
    students = load_data()
    if not students:
        print("当前没有学生记录。")
        return
    print(f"\n{'='*50}")
    print(f"{'学号':<12} {'姓名':<8} {'年龄':<6} {'成绩':<8}")
    print(f"{'-'*50}")
    for s in students:
        print(f"{s['id']:<12} {s['name']:<8} {s['age']:<6} {s['grade']:<8}")
    print(f"{'='*50}\n")


def query_student():
    """按学号或姓名查询学生"""
    students = load_data()
    if not students:
        print("当前没有学生记录。")
        return
    keyword = input("请输入学号或姓名进行查询: ").strip()
    results = [s for s in students if keyword in s["id"] or keyword in s["name"]]
    if not results:
        print(f"未找到匹配 '{keyword}' 的学生。")
        return
    print(f"\n{'='*50}")
    print(f"{'学号':<12} {'姓名':<8} {'年龄':<6} {'成绩':<8}")
    print(f"{'-'*50}")
    for s in results:
        print(f"{s['id']:<12} {s['name']:<8} {s['age']:<6} {s['grade']:<8}")
    print(f"{'='*50}\n")


# ── 主菜单 ──────────────────────────────────────────

def main():
    while True:
        print("\n" + "=" * 30)
        print("   学生信息管理系统")
        print("=" * 30)
        print("  1. 添加学生")
        print("  2. 删除学生")
        print("  3. 修改学生")
        print("  4. 查询学生")
        print("  5. 显示所有学生")
        print("  0. 退出系统")
        print("=" * 30)
        choice = input("请选择操作: ").strip()

        if choice == "1":
            add_student()
        elif choice == "2":
            delete_student()
        elif choice == "3":
            update_student()
        elif choice == "4":
            query_student()
        elif choice == "5":
            list_students()
        elif choice == "0":
            print("感谢使用学生信息管理系统，再见！")
            break
        else:
            print("无效输入，请重新选择。")


if __name__ == "__main__":
    main()
