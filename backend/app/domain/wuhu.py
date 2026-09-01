"""芜湖 12345 事项类别、主管部门和路由词表的唯一事实源。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


DEPARTMENTS: list[dict[str, Any]] = [
    {"_id": "dept_city_management", "name": "芜湖市城市管理局", "name_en": "Wuhu Urban Management Bureau", "category": "城市管理"},
    {"_id": "dept_housing_construction", "name": "芜湖市住房和城乡建设局", "name_en": "Wuhu Housing and Urban-Rural Development Bureau", "category": "城乡建设"},
    {"_id": "dept_public_security", "name": "芜湖市公安局", "name_en": "Wuhu Public Security Bureau", "category": "公共安全"},
    {"_id": "dept_public_services", "name": "芜湖市民政局", "name_en": "Wuhu Civil Affairs Bureau", "category": "公共服务"},
    {"_id": "dept_transportation", "name": "芜湖市交通运输局", "name_en": "Wuhu Transport Bureau", "category": "交通运输"},
    {"_id": "dept_economy_trade", "name": "芜湖市发展和改革委员会", "name_en": "Wuhu Development and Reform Commission", "category": "经济财贸"},
    {"_id": "dept_education_science_culture_sports", "name": "芜湖市教育局", "name_en": "Wuhu Education Bureau", "category": "科教文体"},
    {"_id": "dept_labor_social_security", "name": "芜湖市人力资源和社会保障局", "name_en": "Wuhu Human Resources and Social Security Bureau", "category": "劳动和社会保障"},
    {"_id": "dept_agriculture_forestry_water", "name": "芜湖市农业农村局", "name_en": "Wuhu Agriculture and Rural Affairs Bureau", "category": "农林水土"},
    {"_id": "dept_ecology_environment", "name": "芜湖市生态环境局", "name_en": "Wuhu Ecology and Environment Bureau", "category": "生态环境"},
    {"_id": "dept_market_regulation", "name": "芜湖市市场监督管理局", "name_en": "Wuhu Market Regulation Bureau", "category": "市场监管"},
    {"_id": "dept_health", "name": "芜湖市卫生健康委员会", "name_en": "Wuhu Health Commission", "category": "卫生健康"},
]

CATEGORY_TO_DEPT = {item["category"]: item["_id"] for item in DEPARTMENTS}
DEPARTMENT_NAMES = {item["_id"]: item["name"] for item in DEPARTMENTS}

DEPARTMENT_KEYWORDS: dict[str, list[str]] = {
    "dept_city_management": ["占道", "违建", "市容", "环卫", "垃圾", "油烟", "城管"],
    "dept_housing_construction": ["物业", "住房", "建筑", "施工", "房屋安全", "燃气", "排水"],
    "dept_public_security": ["户口", "身份证", "护照", "治安", "交警", "诈骗", "公安"],
    "dept_public_services": ["低保", "社会救助", "养老服务", "残疾", "民政", "流浪救助"],
    "dept_transportation": ["公交", "出租车", "网约车", "道路运输", "顺风车", "交通运输"],
    "dept_economy_trade": ["价格", "收费", "电价", "水价", "天然气价", "补贴", "消费券", "发改"],
    "dept_education_science_culture_sports": ["招生", "入学", "校外培训", "教育", "旅游", "文化", "体育场馆"],
    "dept_labor_social_security": ["工资", "欠薪", "劳动", "社保", "医保", "就业", "工伤", "人社"],
    "dept_agriculture_forestry_water": ["宅基地", "农村", "农业", "林业", "供水", "节水", "水务", "不动产"],
    "dept_ecology_environment": ["噪声", "污染", "废气", "污水", "环保", "生态环境"],
    "dept_market_regulation": ["12315", "商品质量", "价格欺诈", "消费维权", "食品安全", "市场监管"],
    "dept_health": ["医院", "就医", "卫生", "疫苗", "生育", "计划生育", "健康"],
}

LEGACY_DEPARTMENT_IDS = {
    "dept_jwc", "dept_xsc", "dept_cwc", "dept_rsc", "dept_yjsy", "dept_zfxy", "dept_hqaq", "dept_hqc",
}


async def seed_wuhu_departments(store) -> int:
    now = datetime.now(timezone.utc).isoformat()
    created = 0
    for template in DEPARTMENTS:
        existing = await store.get("departments", template["_id"])
        dept = {**(existing or {}), **template}
        dept.setdefault("admin_users", [])
        dept.setdefault("agent_config", {"model": "deepseek-v4-flash", "temperature": 0.1, "max_tokens": 2048})
        dept.setdefault("loop_phase", "human_in_loop")
        dept.setdefault("review_stats", {"total": 0, "correct": 0, "accuracy": 0.0})
        dept.setdefault("created_at", now)
        dept["updated_at"] = now
        await store.upsert_department(dept)
        created += int(existing is None)
    return created
