# 🚀 Ali Inventory JIT

Sistema inteligente de gestión de inventario para repuestos automotrices con enfoque **JIT (Just In Time)**, optimización de compras y control de pedidos en tránsito.

---

## 🧠 ¿Qué hace este sistema?

Ali Inventory analiza:

- Ventas históricas (3 años)
- Inventario actual
- Backorder
- Pedidos mensuales
- Pedidos emitidos (base de datos)

Y genera automáticamente:

- 📊 Clasificación ABC
- 📦 Pedido inteligente optimizado
- ⚠️ Alertas de reposición
- 💀 Stock muerto
- 🔥 Ofertas sugeridas
- 🚛 Control de pedidos en tránsito (clave JIT)

---

## 🏢 Empresas soportadas

Al iniciar, elegís:

- **Magna**
  - Mazda
  - Kia / Hyundai
  - BMW / MINI
  - Multimarca

- **Alimatico SRL**
  - Multimarca completa

---

## 🔍 Clasificación automática por código

El sistema detecta la marca automáticamente:

| Tipo | Ejemplo |
|------|--------|
| Mazda | B631-14-302A |
| Kia | 77004E500 |
| BMW | 11238511371 |
| Multimarca | ATA.MICRO |

---

## ⚙️ Lógica JIT (CORE DEL SISTEMA)

El sistema calcula:

### 📈 Consumo
