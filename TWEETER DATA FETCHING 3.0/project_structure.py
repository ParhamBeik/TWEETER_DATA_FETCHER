import os

# --- تنظیمات ---
EXCLUDE_DIRS = {'.git', '__pycache__', 'venv', '.venv', '.idea', '.vscode'}

def is_target_file(filename):
    """بررسی می‌کند که آیا فایل مورد نظر، یک فایل اسکریپت یا کانفیگ معتبر است یا خیر"""
    name_lower = filename.lower()
    
    # اسکریپت‌های پایتون و دیتابیس
    if name_lower.endswith('.py') or name_lower.endswith('.sql'):
        return True
        
    # فایل‌های کانفیگ JSON (مثلاً config.json یا search_config.json)
    if name_lower.endswith('.json') and 'config' in name_lower:
        return True
        
    # سایر فایل‌های استاندارد پروژه (پسوند md. از اینجا حذف شد)
    if name_lower.endswith(('.env', '.yaml', '.yml')):
        return True
        
    return False

def has_target_files(dir_path):
    """بررسی می‌کند که آیا در این پوشه یا زیرپوشه‌های عمیق‌تر آن، فایل هدفی وجود دارد؟"""
    for root, dirs, files in os.walk(dir_path):
        # حذف پوشه‌های غیرمجاز از جستجو
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if is_target_file(f):
                return True
    return False

def generate_smart_tree(startpath):
    """تولید ساختار درختی با منطق جستجوی عمقی برای اسکریپت‌ها و کانفیگ‌ها"""
    tree_lines = []
    
    def build_tree(current_path, prefix=""):
        try:
            items = os.listdir(current_path)
        except PermissionError:
            return
            
        dirs = []
        files = []
        
        # تفکیک پوشه‌ها و فایل‌های مجاز
        for item in items:
            path = os.path.join(current_path, item)
            if os.path.isdir(path):
                if item not in EXCLUDE_DIRS:
                    dirs.append(item)
            else:
                if is_target_file(item):
                    files.append(item)
                    
        dirs.sort()
        files.sort()
        
        all_items = dirs + files
        
        for index, item in enumerate(all_items):
            path = os.path.join(current_path, item)
            is_last = (index == len(all_items) - 1)
            connector = "└── " if is_last else "├── "
            
            if os.path.isdir(path):
                if has_target_files(path):
                    # اگر پوشه منتهی به اسکریپت/کانفیگ می‌شود -> بازش کن
                    tree_lines.append(f"{prefix}{connector}📁 {item}/")
                    extension_prefix = "    " if is_last else "│   "
                    build_tree(path, prefix + extension_prefix)
                else:
                    # اگر پوشه فقط حاوی داده/مارک‌داون است -> به صورت بسته نمایش بده
                    tree_lines.append(f"{prefix}{connector}📁 {item}/ [Data/Docs]")
            else:
                # نمایش فایل‌های مجاز
                tree_lines.append(f"{prefix}{connector}📄 {item}")

    tree_lines.append(f"📁 {os.path.basename(os.path.abspath(startpath))}/")
    build_tree(startpath)
    return "\n".join(tree_lines)

def pack_project(startpath):
    """تجمیع کدهای پروژه فقط برای فایل‌های هدف"""
    output = []
    
    for root, dirs, files in os.walk(startpath):
        # فقط وارد پوشه‌هایی می‌شویم که دارای فایل هدف هستند
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and has_target_files(os.path.join(root, d))]
        
        for file in files:
            if is_target_file(file):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        output.append(f'<file path="{path}">\n{content}\n</file>')
                except Exception as e:
                    print(f"Error reading {path}: {e}")
                    
    return "\n\n".join(output)

if __name__ == "__main__":
    print("⏳ در حال پردازش پروژه با منطق جستجوی اسکریپت‌ها...")
    
    tree_text = generate_smart_tree(".")
    with open("structure.txt", "w", encoding="utf-8") as f:
        f.write(tree_text)
    print("✅ ساختار درختی در 'structure.txt' ذخیره شد.")

    packed_code = pack_project(".")
    with open("full_project_context.txt", "w", encoding="utf-8") as f:
        f.write(packed_code)
    print("✅ کدهای پروژه در 'full_project_context.txt' تجمیع شد.")
