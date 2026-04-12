# Ali Inventory

Sistema inteligente de análisis y gestión de repuestos (Mazda, Kia y multimarca).

## 🚀 Funcionalidades

- 📊 Análisis de ventas (3 años)
- 📦 Integración con inventario
- 🚚 Backorder + pedidos en tránsito
- 🧠 Clasificación ABC automática
- 💀 Detección de stock muerto
- 🔥 Sugerencia de ofertas (2 a 2.5 años de stock)
- 📈 Pedido mensual sugerido
- ⚠ Alertas de reposición

---

## 🧠 Lógica del sistema

### 📊 Clasificación ABC
- A → 80% de las ventas
- B → 80% a 95%
- C → 95% a 100%

### 💀 Stock muerto
- Productos con stock
- Sin ventas en los últimos 3 años

### 🔥 Ofertas sugeridas
- Productos con cobertura entre 24 y 30 meses

### 📦 Pedido mensual
- Basado en consumo promedio mensual
- Ajustado por stock actual + backorder + pedidos

---

## ⚙️ Instalación

```bash
pip install -r requirements.txt
