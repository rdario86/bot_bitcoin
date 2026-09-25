import ccxt
import pandas as pd
import time
from datetime import datetime
import pytz
import math # 🌟 Requerido para evitar redondeos peligrosos

# ==========================================
# 1. CONFIGURACIÓN DEL USUARIO
# ==========================================
API_KEY = 'WJkk2CHt59Gv3iphdXf03NozkFYlxwaCrNqFi9or9CxPWHTRNmUA72O3uCrXg98ZUCoNpXqw5gfJdZ1u7UQ'
API_SECRET = 'sOq1gjKraySQWcu34ZkW8SqoSCwZiWzR8I7VCXAZDmi88xVMLkQye6ZQiOsdEGvgt4KONydDXsFRujxlT1FTQ'

SIMBOLO = 'BTC/USDT:USDT'
APALANCAMIENTO = 25 # Apalancamiento Fijo
RATIO_RR = 2.5      # Ratio Beneficio (1:2.50)

# Conexión a BingX
exchange = ccxt.bingx({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

tz_ny = pytz.timezone('America/New_York')

# ==========================================
# 2. FUNCIONES PRINCIPALES
# ==========================================
def obtener_velas(simbolo, timeframe='1m', limite=15):
    try:
        velas = exchange.fetch_ohlcv(simbolo, timeframe=timeframe, limit=limite)
        df = pd.DataFrame(velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Timestamp', inplace=True)
        df.index = df.index.tz_localize('UTC').tz_convert(tz_ny)
        return df
    except Exception as e:
        print(f"Error descargando velas: {e}")
        return pd.DataFrame()

def ejecutar_orden(simbolo, tipo_trade, entrada, stop_loss, take_profit):
    try:
        try:
            exchange.set_leverage(APALANCAMIENTO, simbolo)
        except Exception:
            pass 
            
        balance_info = exchange.fetch_balance()
        balance_usdt = balance_info.get('USDT', {}).get('free', 0.0)
        
        if balance_usdt <= 0:
            print("❌ Error: No hay balance USDT disponible para operar.")
            return False

        # 🌟 BUFFER DE SEGURIDAD (Dejamos un 5% libre para comisiones y volatilidad)
        balance_utilizable = balance_usdt * 0.95

        riesgo_precio = abs(entrada - stop_loss)
        if riesgo_precio == 0: riesgo_precio = 0.01 
        
        sl_porcentaje = (riesgo_precio / entrada) * 100
        riesgo_cuenta_pct = sl_porcentaje * APALANCAMIENTO
        monto_a_arriesgar = balance_utilizable * (riesgo_cuenta_pct / 100)
        
        # 🌟 TRUNCADO HACIA ABAJO (Corta los decimales, NO los redondea)
        cantidad_bruta = monto_a_arriesgar / riesgo_precio
        cantidad = math.floor(cantidad_bruta * 10000) / 10000 
        
        if cantidad <= 0:
            print("❌ Error: Capital insuficiente para comprar el mínimo permitido de BTC.")
            return False
            
        lado_entrada = 'buy' if tipo_trade == 'Long' else 'sell'
        lado_salida = 'sell' if tipo_trade == 'Long' else 'buy'
        position_side = 'LONG' if tipo_trade == 'Long' else 'SHORT'
        
        print(f"💰 [CAPITAL] Balance Utilizable: ${balance_utilizable:.2f} USDT")
        print(f"🧮 [DEBUG] Tamaño SL: {sl_porcentaje:.2f}% | Apalancamiento: {APALANCAMIENTO}x")
        print(f"⚠️ [RIESGO] Si toca SL perderás: {riesgo_cuenta_pct:.2f}% del capital (${monto_a_arriesgar:.2f} USDT)")
        print(f"🚀 Lanzando orden REAL {lado_entrada.upper()} por {cantidad} BTC...")
        
        # 1. ORDEN MARKET DE ENTRADA
        orden = exchange.create_order(
            symbol=simbolo, 
            type='MARKET', 
            side=lado_entrada, 
            amount=cantidad, 
            params={'positionSide': position_side}
        )
        print(f"✅ ¡Posición abierta! ID: {orden.get('id')}")
        
        time.sleep(2) 
        
        # 2. COLOCAR STOP LOSS 
        print(f"🛡️ Colocando Stop Loss estructural en: {stop_loss}")
        try:
            exchange.create_order(
                symbol=simbolo, 
                type='STOP_MARKET', 
                side=lado_salida, 
                amount=cantidad, 
                params={'positionSide': position_side, 'stopPrice': stop_loss}
            )
            print("✅ Stop Loss automatizado con éxito.")
        except Exception as e:
            print(f"⚠️ Error al colocar SL automático: {e}")
            
        # 3. COLOCAR TAKE PROFIT 
        print(f"🎯 Colocando Take Profit en: {take_profit}")
        try:
            exchange.create_order(
                symbol=simbolo, 
                type='TAKE_PROFIT_MARKET', 
                side=lado_salida, 
                amount=cantidad, 
                params={'positionSide': position_side, 'stopPrice': take_profit}
            )
            print("✅ Take Profit automatizado con éxito.")
        except Exception as e:
            print(f"⚠️ Error al colocar TP automático: {e}")
            
        return True
    except Exception as e:
        print(f"❌ Error crítico en el Exchange: {e}")
        return False

def cerrar_posiciones_emergencia(simbolo, hora_texto="11:00"):
    try:
        print(f"🕒 {hora_texto} NY alcanzadas. Cancelando órdenes y cerrando posiciones...")
        
        exchange.cancel_all_orders(simbolo)
        print("✅ Órdenes pendientes canceladas.")
        
        posiciones = exchange.fetch_positions([simbolo])
        
        for pos in posiciones:
            cantidad = float(pos.get('contracts', 0))
            
            if cantidad > 0:
                lado = pos['side']
                lado_cierre = 'sell' if lado == 'long' else 'buy'
                position_side = 'LONG' if lado == 'long' else 'SHORT'
                
                print(f"🔄 Cerrando posición {position_side} de {cantidad} BTC...")
                exchange.create_order(
                    symbol=simbolo,
                    type='MARKET',
                    side=lado_cierre,
                    amount=cantidad,
                    params={'positionSide': position_side}
                )
                print(f"✅ Cierre por fin de sesión ({hora_texto}) completado con éxito.")
                
    except Exception as e:
        print(f"❌ Error cerrando posición: {e}")

# ==========================================
# 3. BUCLE DEL BOT (VIGÍA)
# ==========================================
def iniciar_bot():
    print("🤖 BOT ORB (Stop Dinámico) - Blindaje Anti-Spam Activado. Esperando...")
    
    trade_abierto_hoy = False
    estado_ruptura = None
    pullback_hecho = False
    
    tiempo_ruptura = None
    tiempo_pullback = None
    
    max_orb = None
    min_orb = None
    
    # 🌟 NUEVA VARIABLE: El SL dinámico que se actualizará en vivo
    stop_loss_estructural = None 
    
    while True:
        try:
            ahora_ny = datetime.now(tz_ny)
            hora_actual = ahora_ny.time()
            
            # Reseteo diario a medianoche
            if hora_actual.hour == 0 and hora_actual.minute == 0:
                trade_abierto_hoy = False
                estado_ruptura = None
                pullback_hecho = False
                tiempo_ruptura = None
                tiempo_pullback = None
                max_orb = None
                min_orb = None
                stop_loss_estructural = None
                print(f"🔄 Reseteo diario de variables: {ahora_ny.date()}")
                time.sleep(60)
            
            if ahora_ny.weekday() >= 5:
                time.sleep(3600)
                continue
            
            if trade_abierto_hoy and hora_actual.hour == 11 and hora_actual.minute == 0:
                cerrar_posiciones_emergencia(SIMBOLO, "11:00")
                time.sleep(60)
                continue

            if hora_actual.hour == 9 and hora_actual.minute == 35 and max_orb is None:
                df = obtener_velas(SIMBOLO, timeframe='1m', limite=10)
                if not df.empty:
                    hora_inicio = pd.to_datetime('09:30').time()
                    hora_fin = pd.to_datetime('09:34').time()
                    velas_orb = df.loc[(df.index.time >= hora_inicio) & (df.index.time <= hora_fin)]
                    
                    if len(velas_orb) == 5:
                        max_orb = velas_orb['High'].max()
                        min_orb = velas_orb['Low'].min()
                        print(f"📦 ORB 5m (09:30-09:34) Definida | Techo: {max_orb} | Suelo: {min_orb}")
            
            es_hora_operativa = (hora_actual.hour == 9 and hora_actual.minute >= 35) or (hora_actual.hour == 10 and hora_actual.minute < 30)
            
            if max_orb is not None and es_hora_operativa and not trade_abierto_hoy:
                df = obtener_velas(SIMBOLO, timeframe='1m', limite=5)
                
                if not df.empty:
                    vela_actual = df.iloc[-1] # Vela viva en formación
                    vela_anterior = df.iloc[-2] # Vela recién cerrada
                    
                    # FASE 1: BÚSQUEDA DE RUPTURA
                    if estado_ruptura is None:
                        cierre_ant = vela_anterior['Close']
                        
                        if cierre_ant > max_orb:
                            estado_ruptura = 'Long'
                            stop_loss_estructural = vela_anterior['Low'] # SL inicial
                            tiempo_ruptura = vela_anterior.name 
                            print(f"👀 [Paso 1] Ruptura ALCISTA. SL inicial: {stop_loss_estructural}. Esperando Pullback...")
                            
                        elif cierre_ant < min_orb:
                            estado_ruptura = 'Short'
                            stop_loss_estructural = vela_anterior['High'] # SL inicial
                            tiempo_ruptura = vela_anterior.name 
                            print(f"👀 [Paso 1] Ruptura BAJISTA. SL inicial: {stop_loss_estructural}. Esperando Pullback...")
                    
                    # FASE 2 y 3: RASTREO DE PRECIO, PULLBACK Y CONFIRMACIÓN
                    else:
                        # 🌟 ACTUALIZACIÓN DINÁMICA DEL STOP LOSS EN TIEMPO REAL
                        # Leemos tanto la vela anterior como los mechazos en vivo de la vela actual
                        if estado_ruptura == 'Long':
                            sl_nuevo = min(stop_loss_estructural, vela_anterior['Low'], vela_actual['Low'])
                            if sl_nuevo < stop_loss_estructural:
                                stop_loss_estructural = sl_nuevo
                                print(f"📉 SL Estructural actualizado (Más bajo): {stop_loss_estructural}")
                                
                        elif estado_ruptura == 'Short':
                            sl_nuevo = max(stop_loss_estructural, vela_anterior['High'], vela_actual['High'])
                            if sl_nuevo > stop_loss_estructural:
                                stop_loss_estructural = sl_nuevo
                                print(f"📈 SL Estructural actualizado (Más alto): {stop_loss_estructural}")
                        
                        
                        # LOGICA DE PULLBACK Y CONFIRMACIÓN
                        if not pullback_hecho:
                            if estado_ruptura == 'Long':
                                if vela_actual.name > tiempo_ruptura and vela_actual['Low'] <= max_orb:
                                    pullback_hecho = True
                                    tiempo_pullback = vela_actual.name
                                    print(f"🔥 [Paso 2] ¡Pullback detectado en VIVO! Precio rozó el Techo.")
                                elif vela_anterior.name > tiempo_ruptura and vela_anterior['Low'] <= max_orb:
                                    pullback_hecho = True
                                    tiempo_pullback = vela_anterior.name
                                    print(f"🔥 [Paso 2] ¡Pullback detectado (Vela Cerrada)!")
                                    
                            elif estado_ruptura == 'Short':
                                if vela_actual.name > tiempo_ruptura and vela_actual['High'] >= min_orb:
                                    pullback_hecho = True
                                    tiempo_pullback = vela_actual.name
                                    print(f"🔥 [Paso 2] ¡Pullback detectado en VIVO! Precio rozó el Suelo.")
                                elif vela_anterior.name > tiempo_ruptura and vela_anterior['High'] >= min_orb:
                                    pullback_hecho = True
                                    tiempo_pullback = vela_anterior.name
                                    print(f"🔥 [Paso 2] ¡Pullback detectado (Vela Cerrada)!")
                        
                        elif pullback_hecho:
                            if estado_ruptura == 'Long':
                                if vela_anterior.name >= tiempo_pullback and vela_anterior['Close'] > max_orb:
                                    print(f"💥 [Paso 3] ¡Confirmación Alcista! Vela cerrada por fuera.")
                                    precio_entrada = vela_anterior['Close']
                                    take_profit = precio_entrada + (abs(precio_entrada - stop_loss_estructural) * RATIO_RR)
                                    
                                    ejecutar_orden(SIMBOLO, 'Long', precio_entrada, stop_loss_estructural, take_profit)
                                    
                                    print("🛑 Cacería del día terminada (independientemente del resultado).")
                                    trade_abierto_hoy = True
                                    
                            elif estado_ruptura == 'Short':
                                if vela_anterior.name >= tiempo_pullback and vela_anterior['Close'] < min_orb:
                                    print(f"💥 [Paso 3] ¡Confirmación Bajista! Vela cerrada por debajo.")
                                    precio_entrada = vela_anterior['Close']
                                    take_profit = precio_entrada - (abs(precio_entrada - stop_loss_estructural) * RATIO_RR)
                                    
                                    ejecutar_orden(SIMBOLO, 'Short', precio_entrada, stop_loss_estructural, take_profit)
                                    
                                    print("🛑 Cacería del día terminada (independientemente del resultado).")
                                    trade_abierto_hoy = True

            if max_orb is not None and estado_ruptura is not None and not trade_abierto_hoy and es_hora_operativa:
                time.sleep(2) 
            else:
                time.sleep(20) 
                
        except Exception as e:
            print(f"⚠️ Excepción en el ciclo principal: {e}")
            time.sleep(10)

if __name__ == "__main__":
    iniciar_bot()