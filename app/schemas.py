"""
Pydantic-схемы для валидации входных и выходных данных.
"""
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.identity import RoleName


class UserCreate(BaseModel):
    # Поля role нет: роль при регистрации назначает сервер (всегда student).
    email: EmailStr
    password: str
    phone: str | None = None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    balance: float


class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    phone: str | None
    balance: float
    role: RoleName


class RoleUpdate(BaseModel):
    role: RoleName


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    access_token: str
    refresh_token: str
    token_type: str


class MenuItemCreate(BaseModel):
    name: str
    price: float
    category_id: int | None = None
    description: str | None = Field(default=None, max_length=2000)


class MenuItemUpdate(BaseModel):
    name: str | None = None
    price: float | None = None
    is_available: bool | None = None
    category_id: int | None = None
    description: str | None = Field(default=None, max_length=2000)


class CategoryCreate(BaseModel):
    name: str


class Recipient(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=10, max_length=32)
    location: str = Field(min_length=2, max_length=300)


class KitchenStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^(cooking|ready)$")


class OrderCreate(BaseModel):
    # user_id убран: заказчик определяется по JWT, а не по данным от клиента.
    item_id: int
    quantity: int
    recipient: Recipient | None = None


class DeliveryAssign(BaseModel):
    order_id: int
    courier_id: int
