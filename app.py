import hmac
import logging
import os
import smtplib
import sqlite3
import urllib.parse
from contextlib import closing
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText

import pandas as pd
import streamlit as st

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("goupets")

DB_PATH = "vetpulse.db"
MOTIVOS = ["Vacunación", "Desparasitación", "Control Médico",
           "Peluquería y Estética", "Cirugía / Postoperatorio"]
REPETICIONES = {"No repetir": 0, "En 21 días (refuerzo cachorro)": 21,
                "En 3 meses (desparasitación)": 90, "En 6 meses": 180,
                "En 1 año (vacuna anual)": 365}
CODIGO_PAIS = "57"  # Colombia
DIAS_SIN_REPETIR_CONTACTO = 30


# ---------------------------------------------------------------- Config / secretos
def get_secret(nombre, default=None):
    """Lee de st.secrets o de variables de entorno. Nunca del código."""
    try:
        if nombre in st.secrets:
            return st.secrets[nombre]
    except Exception:
        pass
    return os.environ.get(nombre, default)


# ---------------------------------------------------------------- Base de datos
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _agregar_columna(cur, tabla, columna, definicion):
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({tabla})")]
    if columna not in cols:
        cur.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}")


def init_db():
    with closing(db()) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mascotas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                telefono_dueno TEXT NOT NULL,
                proxima_vacuna TEXT NOT NULL,
                hora_cita TEXT NOT NULL DEFAULT '12:00 AM',
                tipo_alerta TEXT NOT NULL DEFAULT 'Vacunación',
                notificado INTEGER DEFAULT 0,
                fecha_notificado TEXT
            )""")
        _agregar_columna(cur, "mascotas", "hora_cita", "TEXT NOT NULL DEFAULT '12:00 AM'")
        _agregar_columna(cur, "mascotas", "tipo_alerta", "TEXT NOT NULL DEFAULT 'Vacunación'")
        _agregar_columna(cur, "mascotas", "nombre_dueno", "TEXT DEFAULT ''")
        _agregar_columna(cur, "mascotas", "valor", "REAL DEFAULT 0")
        _agregar_columna(cur, "mascotas", "creado_en", "TEXT")
        _agregar_columna(cur, "mascotas", "repetir_dias", "INTEGER DEFAULT 0")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS config (
                clave TEXT PRIMARY KEY,
                valor TEXT
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mensajes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mascota_id INTEGER,
                canal TEXT,
                motivo TEXT,
                enviado_en TEXT
            )""")
        conn.commit()


def get_config(clave, default=""):
    with closing(db()) as conn:
        r = conn.execute("SELECT valor FROM config WHERE clave = ?", (clave,)).fetchone()
    return r["valor"] if r else default


def set_config(clave, valor):
    with closing(db()) as conn:
        conn.execute("INSERT INTO config (clave, valor) VALUES (?, ?) "
                     "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (clave, valor))
        conn.commit()


init_db()


# ---------------------------------------------------------------- Registro de mensajes
def registrar_mensaje(mascota_id, canal, motivo):
    with closing(db()) as conn:
        conn.execute("INSERT INTO mensajes (mascota_id, canal, motivo, enviado_en) VALUES (?,?,?,?)",
                     (mascota_id, canal, motivo, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()


def mensaje_registrado_hoy(mascota_id, motivo):
    hoy = date.today().strftime("%Y-%m-%d")
    with closing(db()) as conn:
        r = conn.execute("SELECT 1 FROM mensajes WHERE mascota_id = ? AND motivo = ? "
                         "AND substr(enviado_en, 1, 10) = ?", (mascota_id, motivo, hoy)).fetchone()
    return r is not None


# ---------------------------------------------------------------- Utilidades
def es_correo(texto):
    return "@" in str(texto)


def limpiar_telefono(texto):
    """Deja solo dígitos y agrega indicativo de país si es un celular de 10 dígitos."""
    digitos = "".join(c for c in str(texto) if c.isdigit())
    if len(digitos) == 10 and digitos.startswith("3"):
        digitos = CODIGO_PAIS + digitos
    return digitos


def enlace_whatsapp(telefono, mensaje):
    return urllib.parse.urlunparse(
        ("https", "wa.me", f"/{telefono}", "", urllib.parse.urlencode({"text": mensaje}), "")
    )


def enviar_correo_smtp(destino, asunto, cuerpo):
    remitente = get_secret("GMAIL_USER")
    password_app = get_secret("GMAIL_APP_PASSWORD")
    if not remitente or not password_app:
        return False, "Faltan GMAIL_USER / GMAIL_APP_PASSWORD en los secretos."
    try:
        msg = MIMEText(cuerpo, "plain", "utf-8")
        msg["Subject"] = asunto
        msg["From"] = remitente
        msg["To"] = destino
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(remitente, password_app)
            server.sendmail(remitente, [destino], msg.as_string())
        return True, ""
    except Exception as e:
        log.exception("Error enviando correo")
        return False, str(e)


def estado_alerta(fecha_str):
    """Devuelve (dias, etiqueta, clase_css) comparando solo fechas."""
    dias = (datetime.strptime(fecha_str, "%Y-%m-%d").date() - date.today()).days
    if dias < 0:
        return dias, f"🔴 VENCIDO (hace {-dias} d)", "red"
    if dias == 0:
        return dias, "🟡 HOY", "yellow"
    if dias == 1:
        return dias, "🟡 MAÑANA", "yellow"
    return dias, f"🟢 Faltan {dias} días", "blue"


def clave_orden(row):
    try:
        h = datetime.strptime(row["hora_cita"], "%I:%M %p").time()
    except ValueError:
        h = datetime.min.time()
    return (row["proxima_vacuna"], h)


def crear_siguiente(f):
    """Si la alerta tiene repetición, crea la siguiente cita. Devuelve la nueva fecha o None."""
    dias = f["repetir_dias"] or 0
    if dias <= 0:
        return None
    base = datetime.strptime(f["proxima_vacuna"], "%Y-%m-%d").date()
    nueva = base + timedelta(days=dias)
    if nueva < date.today():  # si se gestionó muy tarde, cuenta desde hoy
        nueva = date.today() + timedelta(days=dias)
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO mascotas (nombre, nombre_dueno, telefono_dueno, proxima_vacuna, hora_cita, "
            "tipo_alerta, valor, repetir_dias, creado_en) VALUES (?,?,?,?,?,?,?,?,?)",
            (f["nombre"], f["nombre_dueno"] or "", f["telefono_dueno"], nueva.strftime("%Y-%m-%d"),
             f["hora_cita"], f["tipo_alerta"], f["valor"] or 0, dias,
             datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
    return nueva


# ---------------------------------------------------------------- Página
st.set_page_config(page_title="Goupets Pro", page_icon="🐾", layout="wide")

st.markdown("""
<style>
.box-red   {background:#3a1518;border-left:4px solid #ff4b4b;padding:12px 16px;border-radius:8px;}
.box-yellow{background:#3a3315;border-left:4px solid #ffd21f;padding:12px 16px;border-radius:8px;}
.box-blue  {background:#16263d;border-left:4px solid #4b9fff;padding:12px 16px;border-radius:8px;}
</style>
""", unsafe_allow_html=True)

for k, v in {"autenticado": False, "intentos": 0}.items():
    st.session_state.setdefault(k, v)

ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD")
nombre_clinica = get_config("nombre_clinica", "Goupets")
link_pago = get_config("link_pago", "")

# ---- Barra lateral
logo_subido = None
with st.sidebar:
    st.title("⚙️ Goupets Config")
    st.markdown("---")
    if not st.session_state["autenticado"]:
        if not ADMIN_PASSWORD:
            st.error("Configura ADMIN_PASSWORD en .streamlit/secrets.toml")
        else:
            password = st.text_input("Contraseña del Panel", type="password")
            if st.button("Iniciar Sesión", use_container_width=True):
                if st.session_state["intentos"] >= 5:
                    st.error("Demasiados intentos. Recarga la página.")
                elif hmac.compare_digest(password.encode(), str(ADMIN_PASSWORD).encode()):
                    st.session_state["autenticado"] = True
                    st.session_state["intentos"] = 0
                    st.rerun()
                else:
                    st.session_state["intentos"] += 1
                    st.error("Contraseña incorrecta")
    else:
        st.success("🔒 Sesión autorizada")
        st.markdown("---")
        st.subheader("🎨 Identidad del negocio")
        nuevo_nombre = st.text_input("Nombre de la clínica", value=nombre_clinica)
        nuevo_link = st.text_input("Enlace de pago (opcional)", value=link_pago,
                                   placeholder="https://checkout.wompi.co/...")
        if st.button("Guardar configuración", use_container_width=True):
            set_config("nombre_clinica", nuevo_nombre.strip() or "Goupets")
            set_config("link_pago", nuevo_link.strip())
            st.rerun()
        logo_subido = st.file_uploader("Subir logo", type=["png", "jpg", "jpeg"])
        st.markdown("---")
        if st.button("Cerrar sesión", use_container_width=True):
            st.session_state["autenticado"] = False
            st.rerun()

# ---- Todo el contenido exige sesión (datos personales de dueños)
if not st.session_state["autenticado"]:
    st.title("🐾 Goupets - Gestión Veterinaria")
    st.info("Inicia sesión en la barra lateral para continuar.")
    st.stop()

if logo_subido is not None:
    st.image(logo_subido, width=120)
st.title(f"🐾 {nombre_clinica} - Centro de Control e Ingresos")
st.markdown("---")

t_reg, t_sem, t_ina, t_cop, t_met = st.tabs([
    "📋 Registrar Paciente", "🚦 Semáforo de Alertas", "💤 Pacientes Inactivos",
    "📝 Plantillas Clínicas", "📊 Historial y Métricas"])

# ================================================================ TAB 1: Registro
with t_reg:
    st.subheader("Añadir nueva cita / recordatorio")
    with st.form("registro_mascota", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            nombre = st.text_input("Nombre de la mascota")
            dueno = st.text_input("Nombre del dueño")
        with c2:
            contacto = st.text_input("Contacto (WhatsApp o correo)")
            valor = st.number_input("Valor del servicio (COP, opcional)", min_value=0, step=5000)
        tipo_alerta = st.selectbox("Motivo", MOTIVOS)
        repetir = st.selectbox(
            "Programar el siguiente automáticamente al marcar 'Listo'", list(REPETICIONES.keys()))
        cf1, cf2 = st.columns(2)
        with cf1:
            fecha_cita = st.date_input("Fecha programada", min_value=date.today())
        with cf2:
            hora_raw = st.time_input("Hora programada",
                                     value=datetime.strptime("08:00", "%H:%M").time(), step=60)
        guardar = st.form_submit_button("Guardar alerta")

    if guardar:
        if not nombre.strip() or not contacto.strip():
            st.error("Nombre de la mascota y contacto son obligatorios.")
        else:
            contacto_ok = contacto.strip() if es_correo(contacto) else limpiar_telefono(contacto)
            if not es_correo(contacto) and len(contacto_ok) < 10:
                st.error("El número de WhatsApp parece incompleto.")
            else:
                with closing(db()) as conn:
                    conn.execute(
                        "INSERT INTO mascotas (nombre, nombre_dueno, telefono_dueno, proxima_vacuna, "
                        "hora_cita, tipo_alerta, valor, repetir_dias, creado_en) VALUES (?,?,?,?,?,?,?,?,?)",
                        (nombre.strip(), dueno.strip(), contacto_ok, fecha_cita.strftime("%Y-%m-%d"),
                         hora_raw.strftime("%I:%M %p"), tipo_alerta, valor, REPETICIONES[repetir],
                         datetime.now().strftime("%Y-%m-%d %H:%M")))
                    conn.commit()
                st.success("¡Alerta guardada!")

# ================================================================ TAB 2: Semáforo
with t_sem:
    st.subheader("🚦 Panel de control de recordatorios")
    if "aviso" in st.session_state:
        st.success(st.session_state.pop("aviso"))

    with closing(db()) as conn:
        filas = conn.execute("SELECT * FROM mascotas WHERE notificado = 0").fetchall()
    filas = sorted(filas, key=clave_orden)

    if not filas:
        st.info("No hay alertas pendientes.")
    else:
        estados = [estado_alerta(f["proxima_vacuna"]) for f in filas]
        n_venc = sum(1 for e in estados if e[2] == "red")
        n_urg = sum(1 for e in estados if e[2] == "yellow")
        n_prox = sum(1 for e in estados if e[2] == "blue")
        m1, m2, m3 = st.columns(3)
        m1.metric("🔴 Vencidos", n_venc)
        m2.metric("🟡 Hoy / mañana", n_urg)
        m3.metric("🟢 Próximos", n_prox)

        filtro = st.radio("Filtrar", ["Todos", "Vencidos", "Urgentes", "Próximos"], horizontal=True)
        mapa = {"Vencidos": "red", "Urgentes": "yellow", "Próximos": "blue"}

        for f, (dias, etiqueta, color) in zip(filas, estados):
            if filtro != "Todos" and mapa[filtro] != color:
                continue
            pid = f["id"]
            saludo = f"Hola {f['nombre_dueno']}, " if f["nombre_dueno"] else "Hola, "
            msg = (f"{saludo}te recordamos desde {nombre_clinica} el servicio de {f['tipo_alerta']} "
                   f"para {f['nombre']} el día {f['proxima_vacuna']} a las {f['hora_cita']}.")
            if link_pago:
                msg += f" Puedes anticipar tu pago aquí: {link_pago}"

            col_info, col_acc = st.columns([3, 2])
            with col_info:
                st.markdown(f"### 🐕 {f['nombre']}")
                dueno_txt = f" · Dueño: {f['nombre_dueno']}" if f["nombre_dueno"] else ""
                rep_txt = " · 🔁 se repite" if (f["repetir_dias"] or 0) > 0 else ""
                st.markdown(
                    f"<div class='box-{color}'><b>Motivo:</b> {f['tipo_alerta']} | {etiqueta}{dueno_txt}{rep_txt}<br>"
                    f"<b>Programado:</b> {f['proxima_vacuna']} a las {f['hora_cita']}</div>",
                    unsafe_allow_html=True)
            with col_acc:
                st.write("")
                canal = "correo" if es_correo(f["telefono_dueno"]) else "whatsapp"
                if canal == "correo":
                    if st.button("📧 Enviar correo", key=f"mail_{pid}", use_container_width=True):
                        ok, err = enviar_correo_smtp(
                            f["telefono_dueno"], f"🐾 Recordatorio de {f['tipo_alerta']}", msg)
                        if ok:
                            registrar_mensaje(pid, "correo", f["tipo_alerta"])
                            st.success("¡Correo enviado y registrado!")
                        else:
                            st.error(f"Error SMTP: {err}")
                else:
                    st.link_button("📲 Enviar WhatsApp",
                                   enlace_whatsapp(f["telefono_dueno"], msg),
                                   use_container_width=True)
                if st.button("✅ Listo", key=f"done_{pid}", use_container_width=True):
                    with closing(db()) as conn:
                        conn.execute("UPDATE mascotas SET notificado = 1, fecha_notificado = ? WHERE id = ?",
                                     (datetime.now().strftime("%Y-%m-%d %H:%M"), pid))
                        conn.commit()
                    if not mensaje_registrado_hoy(pid, f["tipo_alerta"]):
                        registrar_mensaje(pid, canal, f["tipo_alerta"])
                    nueva = crear_siguiente(f)
                    if nueva:
                        st.session_state["aviso"] = (
                            f"✅ {f['nombre']}: gestionado. Siguiente {f['tipo_alerta']} "
                            f"programado para el {nueva.strftime('%Y-%m-%d')}.")
                    st.rerun()
                with st.expander("📅 Reagendar"):
                    nueva_f = st.date_input("Nueva fecha", min_value=date.today(), key=f"rf_{pid}")
                    nueva_h = st.time_input("Nueva hora", key=f"rh_{pid}",
                                            value=datetime.strptime("08:00", "%H:%M").time(), step=60)
                    if st.button("Guardar cambio", key=f"rs_{pid}", use_container_width=True):
                        with closing(db()) as conn:
                            conn.execute("UPDATE mascotas SET proxima_vacuna = ?, hora_cita = ? WHERE id = ?",
                                         (nueva_f.strftime("%Y-%m-%d"), nueva_h.strftime("%I:%M %p"), pid))
                            conn.commit()
                        st.rerun()
            st.markdown("")

# ================================================================ TAB 3: Pacientes inactivos
with t_ina:
    st.subheader("💤 Pacientes inactivos: recupéralos")
    dias_inact = st.slider("Sin actividad hace más de (días)", 30, 365, 90, step=15)
    mostrar_contactados = st.checkbox(f"Mostrar también los contactados en los últimos {DIAS_SIN_REPETIR_CONTACTO} días")

    hoy = date.today()
    hoy_str = hoy.strftime("%Y-%m-%d")
    with closing(db()) as conn:
        todas = [dict(r) for r in conn.execute("SELECT * FROM mascotas")]
        contactos = {r["mascota_id"]: r["ultimo"] for r in conn.execute(
            "SELECT mascota_id, MAX(enviado_en) AS ultimo FROM mensajes "
            "WHERE motivo = 'Reactivación' GROUP BY mascota_id")}

    grupos = {}
    for r in todas:
        k = (r["nombre"].strip().lower(), r["telefono_dueno"])
        g = grupos.setdefault(k, {"nombre": r["nombre"], "dueno": r["nombre_dueno"] or "",
                                  "contacto": r["telefono_dueno"], "ultima": "", "ultimo_id": None,
                                  "pendiente": False, "ult_contacto": ""})
        if r["proxima_vacuna"] > g["ultima"]:
            g["ultima"], g["ultimo_id"] = r["proxima_vacuna"], r["id"]
        if r["notificado"] == 0 and r["proxima_vacuna"] >= hoy_str:
            g["pendiente"] = True
        g["ult_contacto"] = max(g["ult_contacto"], contactos.get(r["id"], ""))

    inactivos = []
    for g in grupos.values():
        sin_actividad = (hoy - datetime.strptime(g["ultima"], "%Y-%m-%d").date()).days
        if g["pendiente"] or sin_actividad < dias_inact:
            continue
        reciente = False
        if g["ult_contacto"]:
            ult = datetime.strptime(g["ult_contacto"][:10], "%Y-%m-%d").date()
            reciente = (hoy - ult).days < DIAS_SIN_REPETIR_CONTACTO
        if reciente and not mostrar_contactados:
            continue
        g["dias"], g["reciente"] = sin_actividad, reciente
        inactivos.append(g)
    inactivos.sort(key=lambda g: -g["dias"])

    st.metric("Pacientes para reactivar", len(inactivos))
    if not inactivos:
        st.info("No hay pacientes inactivos con ese criterio. ¡Buen trabajo!")

    for g in inactivos:
        gid = g["ultimo_id"]
        saludo = f"Hola {g['dueno']}, " if g["dueno"] else "Hola, "
        msg = (f"{saludo}desde {nombre_clinica} extrañamos a {g['nombre']} 🐾. "
               "Ya pasó un tiempo desde su última visita, ¿te ayudamos a agendar un control?")
        ci, ca, cb = st.columns([3, 2, 1])
        with ci:
            etiqueta = " ✔ contactado" if g["reciente"] else ""
            st.markdown(f"**🐕 {g['nombre']}**{etiqueta}  \nÚltima cita: {g['ultima']} · hace {g['dias']} días")
        with ca:
            if es_correo(g["contacto"]):
                if st.button("📧 Enviar correo", key=f"ri_mail_{gid}", use_container_width=True):
                    ok, err = enviar_correo_smtp(g["contacto"], f"🐾 Te extrañamos en {nombre_clinica}", msg)
                    if ok:
                        registrar_mensaje(gid, "correo", "Reactivación")
                        st.success("Enviado y registrado.")
                    else:
                        st.error(f"Error SMTP: {err}")
            else:
                st.link_button("📲 Enviar WhatsApp", enlace_whatsapp(g["contacto"], msg),
                               use_container_width=True)
        with cb:
            if not es_correo(g["contacto"]):
                if st.button("✔ Contactado", key=f"ri_ok_{gid}", use_container_width=True):
                    registrar_mensaje(gid, "whatsapp", "Reactivación")
                    st.rerun()
        st.divider()

# ================================================================ TAB 4: Plantillas clínicas
with t_cop:
    st.subheader("📝 Plantillas de seguimiento clínico")
    ci1, ci2 = st.columns(2)
    with ci1:
        ia_nombre = st.text_input("Nombre de la mascota:", placeholder="Ej. Max", key="ia_n")
        ia_contacto = st.text_input("Contacto del dueño (WhatsApp o correo):", key="ia_c")
        ia_diag = st.selectbox("Procedimiento:", [
            "Postoperatorio Castración", "Tratamiento Gastroenteritis", "Higiene Oral Profilaxis"])
        generar = st.button("✨ Generar mensaje", use_container_width=True)
    with ci2:
        if generar:
            if not ia_nombre.strip() or not ia_contacto.strip():
                st.error("Rellena todos los campos.")
            else:
                st.session_state["plantilla"] = {
                    "Postoperatorio Castración":
                        f"🏥 {nombre_clinica}: Hola, ¿cómo evoluciona {ia_nombre}? Recuerda: "
                        "1. Mantener el collar isabelino. 2. Puntos limpios y secos. 3. Dar los analgésicos indicados.",
                    "Tratamiento Gastroenteritis":
                        f"🐾 {nombre_clinica}: Hola, ¿cómo sigue {ia_nombre}? Recuerda: "
                        "1. Agua en sorbos pequeños. 2. Dieta blanda tras 6 horas sin vomitar.",
                    "Higiene Oral Profilaxis":
                        f"🦷 {nombre_clinica}: Cuidados de {ia_nombre} tras la limpieza dental: "
                        "1. Alimento blando hoy. 2. Evitar juguetes rígidos.",
                }[ia_diag]
                st.session_state["plantilla_contacto"] = ia_contacto.strip()

        if "plantilla" in st.session_state:
            texto = st.text_area("Mensaje (editable):", value=st.session_state["plantilla"], height=170)
            dest = st.session_state["plantilla_contacto"]
            if es_correo(dest):
                if st.button("📧 Enviar correo clínico", use_container_width=True):
                    ok, err = enviar_correo_smtp(dest, "Seguimiento clínico", texto)
                    if ok:
                        st.success("Enviado por email.")
                    else:
                        st.error(f"Error SMTP: {err}")
            else:
                st.link_button("📲 Enviar WhatsApp",
                               enlace_whatsapp(limpiar_telefono(dest), texto),
                               use_container_width=True)

# ================================================================ TAB 5: Historial y métricas
with t_met:
    st.subheader("📊 Panel administrativo")
    with closing(db()) as conn:
        df = pd.read_sql_query("SELECT * FROM mascotas", conn)
        df_msg = pd.read_sql_query(
            "SELECT m.id, m.enviado_en, m.canal, m.motivo, p.nombre AS mascota, p.nombre_dueno AS dueno "
            "FROM mensajes m LEFT JOIN mascotas p ON p.id = m.mascota_id ORDER BY m.enviado_en DESC", conn)

    if df.empty:
        st.info("Aún no hay datos.")
    else:
        hechos = df[df["notificado"] == 1]
        pendientes = df[df["notificado"] == 0]
        vencidos = sum(1 for d in pendientes["proxima_vacuna"] if estado_alerta(d)[2] == "red")
        mes_actual = date.today().strftime("%Y-%m")
        n_msg_mes = int((df_msg["enviado_en"].str[:7] == mes_actual).sum()) if not df_msg.empty else 0

        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Total de alertas", len(df))
        k2.metric("Gestionadas", len(hechos))
        k3.metric("Pendientes", len(pendientes))
        k4.metric("Vencidas sin gestionar", vencidos)
        k5.metric("📨 Mensajes este mes", n_msg_mes)
        st.metric("💰 Ingresos asociados a recordatorios gestionados",
                  f"${hechos['valor'].fillna(0).sum():,.0f} COP")

        g1, g2 = st.columns(2)
        with g1:
            st.markdown("**Alertas por motivo**")
            st.bar_chart(df["tipo_alerta"].value_counts())
        with g2:
            st.markdown("**Gestionadas por mes**")
            if not hechos.empty:
                st.bar_chart(hechos["fecha_notificado"].str[:7].value_counts().sort_index())
            else:
                st.caption("Todavía no hay alertas gestionadas.")

        st.markdown("### Historial de alertas gestionadas")
        if hechos.empty:
            st.caption("Sin registros.")
        else:
            vista = hechos[["id", "nombre", "nombre_dueno", "tipo_alerta", "proxima_vacuna",
                            "fecha_notificado", "valor"]].sort_values("fecha_notificado", ascending=False)
            st.dataframe(vista, use_container_width=True, hide_index=True)

            u1, u2 = st.columns([2, 1])
            with u1:
                restaurar = st.selectbox(
                    "Deshacer (volver al semáforo)", vista["id"],
                    format_func=lambda i: f"#{i} · {vista.loc[vista['id'] == i, 'nombre'].iloc[0]}")
            with u2:
                st.write("")
                st.write("")
                if st.button("↩️ Restaurar", use_container_width=True):
                    with closing(db()) as conn:
                        conn.execute("UPDATE mascotas SET notificado = 0, fecha_notificado = NULL WHERE id = ?",
                                     (int(restaurar),))
                        conn.commit()
                    st.rerun()
            st.caption("Si la alerta ya había creado su siguiente cita automática, bórrala del semáforo marcándola 'Listo' o reagéndala.")

        st.markdown("### 📨 Registro de mensajes enviados")
        if df_msg.empty:
            st.caption("Aún no hay mensajes registrados.")
        else:
            st.dataframe(df_msg, use_container_width=True, hide_index=True)

        st.download_button("⬇️ Exportar alertas a CSV",
                           df.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"goupets_{date.today()}.csv", mime="text/csv")
