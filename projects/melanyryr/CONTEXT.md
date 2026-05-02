## Proyecto melanyryr

- Root repo: `/home/develop/development/Jardal/melanyryr`
- Frontend principal: `MelanyRyR_react/`
- Backend principal: `MelanyRyR_rest/`
- PWA adicional: `MelanyRyR_pwa/`

Usar `codex` como provider por defecto para sesiones dev de este proyecto.

## 2026-04-29 04:39 UTC - 8638ace6-7fb9-46bf-a4d2-abe4a2c35f3b
- Title: Corregir ordenamiento de la tabla de transferencias
- Summary: Corregir la tabla de transferencias para que muestre primero las más recientes y para que los botones de ordenamiento funcionen de forma consistente.

## 2026-04-29 05:01 UTC - 5bdaf798-0e7b-41d1-900a-2d7274e37845
- Title: Recalcular saldos automáticamente al editar una venta
- Summary: Toda modificación de una venta debe actualizar automáticamente el stock afectado para evitar desfases de inventario.

## 2026-04-29 05:54 UTC - c5da6526-aa8b-4a6f-a214-e31c3e637ed2
- Title: Mejorar buscador de productos en transferencias
- Summary: Mejorar el buscador de productos en transferencias para encontrar ítems por coincidencia parcial y reducir errores de selección. En los resultados debe mostrarse información suficiente para distinguir variantes similares, incluyendo piezas por caja y saldo.

## 2026-04-29 05:56 UTC - fea25b26-f773-4ea8-a80c-4bbe5e55a631
- Title: Incorporar trazabilidad visible de transferencia
- Summary: Agregar y mostrar la trazabilidad básica de cada transferencia: fecha, número de transferencia y usuario logueado que la realizó. El número de transferencia también debe imprimirse en el documento correspondiente. Si el campo nro_transferencia no existe, debe crearse.

## 2026-04-29 06:04 UTC - 8638ace6-7fb9-46bf-a4d2-abe4a2c35f3b
- Title: Corregir ordenamiento de la tabla de transferencias
- Summary: Corregir la tabla de transferencias para que muestre primero las más recientes y para que los botones de ordenamiento funcionen de forma consistente.

## 2026-04-29 06:10 UTC - f5016c5b-0895-4f1a-b549-7c0cb93f0f6e
- Title: Revertir stock al origen cuando se elimina una transferencia
- Summary: Si una transferencia se elimina por error, el stock debe volver automáticamente a su ubicación de origen.

## 2026-04-29 06:14 UTC - c5da6526-aa8b-4a6f-a214-e31c3e637ed2
- Title: Mejorar buscador de productos en transferencias
- Summary: Mejorar el buscador de productos en transferencias para encontrar ítems por coincidencia parcial y reducir errores de selección. En los resultados debe mostrarse información suficiente para distinguir variantes similares, incluyendo piezas por caja y saldo.

## 2026-04-29 06:31 UTC - 56ef5daf-3aab-4899-8da6-858812c89590
- Title: Catálogo por contenedor mostrando lo que queda
- Summary: Generar un catálogo filtrado por contenedor mostrando el saldo disponible de cada producto.

## 2026-04-29 07:02 UTC - e7f00e40-5277-431a-9871-9772469943e2
- Title: Mantener vendedor original y registrar editor de la venta
- Summary: Cuando una venta se edite, debe mantenerse el vendedor original y además registrarse qué usuario la editó y cuándo lo hizo.

## 2026-04-29 07:12 UTC - 8b87a046-38ef-4d5e-b91f-3c1eeed07ba9
- Title: Permitir hasta 2 imágenes por producto
- Summary: Actualmente cada producto acepta solo 1 imagen. Debe aceptar hasta 2 imágenes por producto.

## 2026-04-29 07:23 UTC - fd7bad5e-6a08-4ab3-bc7d-d94aa4f1fe7f
- Title: Corregir thumbnail del pin del almacén en la lista del mapa
- Summary: En build, el pin o thumbnail del almacén no carga y aparece imagen faltante o genérica. Debe mostrarse el pin rojo correcto. En desarrollo funciona, por lo que probablemente haya un problema de asset o ruta al compilar.

## 2026-04-29 07:40 UTC - e8f87447-3f91-4d26-856c-e79f6809e50d
- Title: Imprimir comprobante de cierre de caja
- Summary: Al cerrar caja, generar un comprobante con datos del cierre como fecha, hora, monto, quién cerró y quién recogió.

## 2026-04-29 07:55 UTC - b812e99a-c23c-411b-b118-d034f70cfb8b
- Title: Mostrar inventario por tienda
- Summary: Permitir consultar el inventario disponible por cada tienda, separado del almacén general.

## 2026-04-29 08:12 UTC - 474964d9-aff3-4040-aaf3-8f35e3d8cc6c
- Title: Resaltar visualmente variantes de productos
- Summary: Marcar visualmente las variantes de productos para evitar errores al seleccionar códigos similares.

## 2026-04-29 08:27 UTC - 8efa4e29-87c8-4785-8620-1d73faa99fc2
- Title: Mejorar legibilidad del código de producto
- Summary: Aumentar la legibilidad visual del código del producto para facilitar su lectura durante la operación diaria.

## 2026-04-29 13:46 UTC - 8b9c77ce-3a73-4c68-8276-d21f7841c4f4
- Title: Restringir edición de transferencias por permisos
- Summary: Limitar la edición de transferencias solo a perfiles autorizados para evitar alteraciones indebidas del inventario.

## 2026-04-29 13:58 UTC - 9952fa6c-ffd1-412c-8846-291d96714767
- Title: Crear flujo rápido de transferencia similar al de ventas
- Summary: Simplificar el flujo de transferencias para que sea más ágil y más parecido al de ventas, reduciendo clics y tiempo operativo.

## 2026-04-29 14:14 UTC - cc1ccccc-fec5-46d6-8994-a9b0dff702d2
- Title: Registrar contenedor en la importación de entradas con código y nombre operativo
- Summary: En la vista de importación Excel donde registran entradas de almacén, agregar el registro del contenedor con dos datos: codigo_contenedor y nombre_contenedor. El nombre del contenedor es operativo y legible para administrativos y trabajadores que controlan inventario. Debe sugerirse automáticamen...

## 2026-04-29 14:27 UTC - e05e2f1d-40b6-434f-a545-dd61b72dd2c4
- Title: Vista rápida por fila para saber dónde están las cajas
- Summary: Desde cada ítem debe existir una vista rápida o desplegable para ver si sus cajas están en almacén, tienda o cliente y poder localizar el destino actual de la mercadería.

## 2026-04-29 14:51 UTC - d1e68be7-771e-4e15-a1a6-6f9407ed240f
- Title: Reporte de trazabilidad de cajas por producto o contenedor
- Summary: Crear un reporte que permita seguir el destino de las cajas por producto o por contenedor, incluyendo cliente o tienda y rango de fechas.

## 2026-04-29 15:10 UTC - 79f4c413-2d16-4f34-99d2-30847b0089f1
- Title: Reporte operativo por contenedor con periodos y monto
- Summary: Crear un reporte operativo por contenedor que muestre cajas ingresadas, cajas salidas, saldo restante y monto económico asociado, con vistas diaria, mensual y anual.

## 2026-04-29 15:27 UTC - 93d2a81c-eec3-41ff-b317-c0ba827841ab
- Title: Reporte acumulado por cliente
- Summary: Permitir consultar la venta acumulada por cliente, incluyendo venta diaria e histórico filtrable por fechas.

## 2026-04-29 15:34 UTC - 1bb4f33e-c733-46fd-989a-0c7f2f7c3f69
- Title: Reporte por contenedor de cajas restantes y clientes compradores
- Summary: Generar un reporte por contenedor que muestre qué cajas quedan y qué clientes compraron mercancía de ese contenedor.

## 2026-04-29 15:59 UTC - 7b025cc5-3bea-48bc-a489-8bee8daf68ba
- Title: Exportar reportes por contenedor en PDF
- Summary: Permitir descargar en PDF los reportes por contenedor para revisión, inventario físico y control operativo.

## 2026-04-29 16:11 UTC - efb8518e-17b8-4dd6-8f82-57255f414db9
- Title: Mantener visibles en ventas los productos con saldo parcial
- Summary: Cuando quedan piezas parciales o media caja, el producto no debe desaparecer de ventas si aún existe saldo real.

## 2026-04-29 16:27 UTC - 3d58163f-c2dc-4e03-b207-601f1c511cb7
- Title: Reiniciar inventario sin perder histórico
- Summary: Permitir cerrar el inventario histórico hasta una fecha y comenzar un nuevo estado base limpio sin borrar la información anterior.

## 2026-04-29 16:39 UTC - 31c3e309-c780-4aff-bbf9-5aa46a617404
- Title: Ajuste de saldo a cero valorizando el restante como salida a Administración
- Summary: Al aplicar una limpieza o reinio de inventario, todo lo que quede como restante debe registrarse como salida o venta a Administración usando el precio por caja, dejando trazabilidad del ajuste.

## 2026-04-29 16:56 UTC - c0558628-e668-4a31-8e4d-67af65e066f2
- Title: Vista rápida estilo Excel para edición masiva de precios por contenedor
- Summary: Crear una vista rápida para editar precios de todos los ítems de un contenedor en una sola pantalla. Debe sentirse lo más parecido posible a una hoja de cálculo: filas continuas, edición directa en celdas o inputs, navegación rápida con teclado y guardado masivo. En esa misma vista los productos ...

## 2026-04-29 17:17 UTC - b04327db-a9a7-49e9-bbdc-e398c2dcf9a0
- Title: Vista rápida estilo Excel para conteo y edición de cajas por contenedor
- Summary: Crear una vista rápida para revisar y editar cantidades de cajas de todos los ítems de un contenedor en una sola pantalla. Debe sentirse similar a Excel para que el equipo pueda contar, corregir y avanzar rápido sin abrir producto por producto.

## 2026-04-29 17:43 UTC - 469fa9fb-aa19-42ce-89c4-95a9a1da4418
- Title: Alertar clientes duplicados mostrando quién creó el registro existente
- Summary: Al crear un cliente, el sistema debe advertir si ya existe otro con el mismo nombre o uno muy similar y mostrar quién creó el registro existente.

## 2026-04-29 17:50 UTC - 45414450-bb32-4a99-9289-458911af7723
- Title: Completar metadatos de impresión en la nota de venta
- Summary: La nota de venta debe incluir celular del cliente, fecha de creación, fecha de impresión, contador de impresiones y registro de usuario que imprimió. Esto ayuda a evitar errores de despacho duplicado y mejora el seguimiento documental.

## 2026-04-29 18:05 UTC - 2914b399-480e-4ac8-8745-1b4d125baf2f
- Title: Mejorar el catálogo de impresión para cliente
- Summary: Hacer más presentable el catálogo para cliente. El orden esperado es foto, ítem, descripción, piezas por caja y total de cajas, con opción de filtrar por rango de fechas.

## 2026-04-29 18:43 UTC - 0de907ff-07e8-4c97-82a9-ae04b5707114
- Title: Generar detalle de deuda del cliente en PDF
- Summary: Generar un PDF con el detalle de deuda del cliente para compartirlo fácilmente.

## 2026-04-29 18:53 UTC - ba1da4f2-3bd6-435b-ad1d-9544c4ac0e1e
- Title: Agregar botón de WhatsApp del cliente con enlace wa.me
- Summary: Agregar un botón que abra el chat del cliente usando un enlace wa.me/591XXXXXXXX. No debe usar APIs externas ni automatización; solo abrir el chat correspondiente.

## 2026-04-29 19:10 UTC - c72abf2a-c531-4dcb-9748-977f79985271
- Title: Mejorar detalle de caja efectivo
- Summary: Mejorar la claridad visual y operativa del detalle de efectivo en caja para facilitar revisión y control.

## 2026-04-29 19:39 UTC - c5da6526-aa8b-4a6f-a214-e31c3e637ed2
- Title: Mejorar buscador de productos en transferencias
- Summary: Mejorar el buscador de productos en transferencias para encontrar ítems por coincidencia parcial y reducir errores de selección. En los resultados debe mostrarse información suficiente para distinguir variantes similares, incluyendo piezas por caja y saldo.

## 2026-04-29 19:49 UTC - 1bf325d5-6fb8-48ca-b9a3-167e9268aa14
- Title: Mejorar la edición de ventas para que se comporte como crear venta
- Summary: Hacer que la pantalla de edición de ventas se parezca a la de creación, mostrando el precio por caja como referencia sin permitir editarlo y corrigiendo el bug que obliga a seleccionar dos veces el almacén u origen.

## 2026-04-29 20:10 UTC - 5bdaf798-0e7b-41d1-900a-2d7274e37845
- Title: Recalcular saldos automáticamente al editar una venta
- Summary: Toda modificación de una venta debe actualizar automáticamente el stock afectado para evitar desfases de inventario.

## 2026-05-01 02:49 UTC - e05e2f1d-40b6-434f-a545-dd61b72dd2c4
- Title: Vista rápida por fila para saber dónde están las cajas
- Summary: Desde cada ítem debe existir una vista rápida o desplegable para ver si sus cajas están en almacén, tienda o cliente y poder localizar el destino actual de la mercadería.

## 2026-05-01 02:55 UTC - 79f4c413-2d16-4f34-99d2-30847b0089f1
- Title: Reporte operativo por contenedor con periodos y monto
- Summary: Crear un reporte operativo por contenedor que muestre cajas ingresadas, cajas salidas, saldo restante y monto económico asociado, con vistas diaria, mensual y anual.

## 2026-05-01 03:04 UTC - 93d2a81c-eec3-41ff-b317-c0ba827841ab
- Title: Reporte acumulado por cliente
- Summary: Permitir consultar la venta acumulada por cliente, incluyendo venta diaria e histórico filtrable por fechas.
