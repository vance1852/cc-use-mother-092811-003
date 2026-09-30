"""领域服务使用的业务异常。"""


class DomainError(Exception):
    """所有可预期业务异常的基类。"""

    code = "domain_error"
    status = 400


class ValidationError(DomainError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class PermissionDenied(DomainError):
    """操作者没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class ConflictError(DomainError):
    """请求编号或业务唯一键与既有内容冲突。"""

    code = "conflict"
    status = 409


class PlanningError(DomainError):
    """补能计划无法生成或当前状态不允许该动作。"""

    code = "planning_error"
    status = 422


class CapacityError(DomainError):
    """站点时隙容量不足，无法锁定。"""

    code = "capacity_exceeded"
    status = 409


class StaleVersionError(DomainError):
    """道路版本、计划有效期或设备状态已变化。"""

    code = "stale_version"
    status = 409
