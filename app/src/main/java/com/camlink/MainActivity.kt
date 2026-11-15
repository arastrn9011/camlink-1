package com.camlink

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.SurfaceTexture
import android.graphics.Typeface
import android.graphics.YuvImage
import android.graphics.drawable.GradientDrawable
import android.hardware.Camera
import android.net.wifi.WifiManager
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.view.Gravity
import android.view.TextureView
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.thread

const val PORT = 5000
private const val MATCH = ViewGroup.LayoutParams.MATCH_PARENT
private const val WRAP = ViewGroup.LayoutParams.WRAP_CONTENT
private val RESTART_KEYS = setOf("cam", "res", "fps", "preview", "vfps")

// Protokol: paket = [tip:1][uzunluk:4 big-endian][veri]
// Telefon -> PC: 9=bilgi 2=jpeg kare 8=yayın durdu 10=durum+yetenekler(json) 12=istatistik(json)
// PC -> Telefon: 20=komut(json)
@Suppress("DEPRECATION")
class MainActivity : Activity() {
    // ---- ağ ----
    private val lock = Any()
    @Volatile private var client: Socket? = null
    @Volatile private var info: ByteArray? = null
    @Volatile private var caps: JSONObject? = null
    private val ctl: ExecutorService = Executors.newSingleThreadExecutor()

    // ---- durum ----
    @Volatile private var running = false
    @Volatile private var gen = 0
    @Volatile private var jqNow = 85
    private var cfg = JSONObject()
    private val capCount = AtomicLong()
    private val sentCount = AtomicLong()
    private val dropCount = AtomicLong()
    private val jpegNs = AtomicLong()
    private val jpegN = AtomicLong()
    private var lastT = System.nanoTime()
    private var lastCap = 0L
    private var lastSent = 0L
    private var lastJn = 0L
    private var lastJns = 0L

    // ---- kamera ----
    private var cam1: Camera? = null
    private var dummyTex: SurfaceTexture? = null
    private var workPool: ExecutorService? = null
    private var seqCounter = 0L
    private var turn = 0L
    private val orderLock = ReentrantLock()
    private val orderCond = orderLock.newCondition()
    private val tlBos = ThreadLocal<ByteArrayOutputStream>()
    private lateinit var camThread: HandlerThread
    private lateinit var camHandler: Handler
    private var wifiLock: WifiManager.WifiLock? = null

    // ---- arayüz ----
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var prefs: SharedPreferences
    private var dark = true
    private val accent = 0xFF4C8DFF.toInt()
    private val cards = ArrayList<View>()
    private val primaryTexts = ArrayList<TextView>()
    private val subTexts = ArrayList<TextView>()
    private val buttons = ArrayList<Pair<Button, Boolean>>()
    private val lines = ArrayList<String>()
    private lateinit var scroll: ScrollView
    private lateinit var statusTv: TextView
    private lateinit var ipTv: TextView
    private lateinit var statsTv: TextView
    private lateinit var logTv: TextView
    private lateinit var tv: TextureView
    private lateinit var bStart: Button
    private lateinit var settingsCard: LinearLayout
    private lateinit var previewRow: LinearLayout

    private fun bg() = if (dark) 0xFF0F1115.toInt() else 0xFFF2F4F8.toInt()
    private fun cardBg() = if (dark) 0xFF1A1D24.toInt() else 0xFFFFFFFF.toInt()
    private fun fg() = if (dark) 0xFFE8EAF0.toInt() else 0xFF151821.toInt()
    private fun sub() = if (dark) 0xFF8B93A7.toInt() else 0xFF5B6478.toInt()
    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
    private fun round(color: Int, r: Int) = GradientDrawable().apply { setColor(color); cornerRadius = dp(r).toFloat() }

    private fun defaults() = JSONObject().apply {
        put("cam", 0); put("res", "1280x720"); put("fps", 30); put("jq", 85); put("preview", false)
        put("vfps", false); put("focus", "continuous-video"); put("exp", 0); put("ae_lock", false)
        put("awb_lock", false); put("wb", "auto"); put("zoom", 0); put("torch", false)
        put("anti", "auto"); put("iso", "auto"); put("stab", false)
    }

    private fun saveCfg() { prefs.edit().putString("cfg", cfg.toString()).apply() }

    private fun log(msg: String) {
        ui.post {
            lines.add(msg)
            while (lines.size > 5) lines.removeAt(0)
            logTv.text = lines.joinToString("\n")
        }
    }

    // ---------- arayüz kurulumu ----------
    private fun text(t: String, size: Float, bold: Boolean = false, secondary: Boolean = false): TextView =
        TextView(this).apply {
            text = t
            textSize = size
            if (bold) setTypeface(typeface, Typeface.BOLD)
            if (secondary) subTexts.add(this) else primaryTexts.add(this)
        }

    private fun card(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(16), dp(14), dp(16), dp(14))
        layoutParams = LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = dp(12) }
        cards.add(this)
    }

    private fun button(label: String, secondary: Boolean = false, onClick: () -> Unit): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            textSize = 16f
            stateListAnimator = null
            setOnClickListener { onClick() }
            buttons.add(this to secondary)
        }

    private fun switchRow(title: String, initial: Boolean, onChange: (Boolean) -> Unit): LinearLayout {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(8), 0, dp(8))
        }
        row.addView(text(title, 15f), LinearLayout.LayoutParams(0, WRAP, 1f))
        val sw = Switch(this)
        sw.isChecked = initial
        sw.setOnCheckedChangeListener { _, v -> onChange(v) }
        row.addView(sw)
        return row
    }

    private fun buildUi() {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(12), dp(16), dp(28))
        }
        val header = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        header.addView(text("CamLink", 26f, bold = true), LinearLayout.LayoutParams(0, WRAP, 1f))
        header.addView(button("Ayarlar", secondary = true) {
            settingsCard.visibility = if (settingsCard.visibility == View.VISIBLE) View.GONE else View.VISIBLE
        })
        root.addView(header)

        val c1 = card()
        statusTv = TextView(this).apply { textSize = 20f; setTypeface(typeface, Typeface.BOLD) }
        ipTv = text("", 14f, secondary = true)
        statsTv = text("", 13f, secondary = true)
        logTv = text("", 11f, secondary = true)
        c1.addView(statusTv); c1.addView(ipTv); c1.addView(statsTv); c1.addView(logTv)
        root.addView(c1)

        tv = TextureView(this).apply {
            layoutParams = LinearLayout.LayoutParams(MATCH, dp(2)).apply { topMargin = dp(12) }
        }
        root.addView(tv)

        bStart = button("Başlat") { if (running) stopAll() else ensureStart() }
        root.addView(bStart, LinearLayout.LayoutParams(MATCH, WRAP).apply { topMargin = dp(12) })

        settingsCard = card()
        settingsCard.visibility = View.GONE
        settingsCard.addView(text("Uygulama ayarları", 16f, bold = true))
        settingsCard.addView(switchRow("Koyu tema", dark) { v ->
            dark = v; prefs.edit().putBoolean("dark", v).apply(); applyTheme()
        })
        settingsCard.addView(switchRow("Açılışta otomatik başlat", prefs.getBoolean("autostart", true)) { v ->
            prefs.edit().putBoolean("autostart", v).apply()
        })
        settingsCard.addView(switchRow("Ekranı karart (pil ve ısı için)", prefs.getBoolean("dim", false)) { v ->
            prefs.edit().putBoolean("dim", v).apply(); applyDim()
        })
        previewRow = switchRow("Telefon ekranında önizleme", cfg.optBoolean("preview", false)) { v ->
            handleCmd(JSONObject().put("preview", v))
        }
        settingsCard.addView(previewRow)
        settingsCard.addView(text("Kamera, kalite, odak, pozlama ve diğer tüm ayarlar bilgisayar programından yapılır.", 12f, secondary = true))
        settingsCard.addView(text("CamLink 3.0  •  Port $PORT", 12f, secondary = true))
        root.addView(settingsCard)

        scroll = ScrollView(this).apply { addView(root) }
        setContentView(scroll)
    }

    private fun applyTheme() {
        scroll.setBackgroundColor(bg())
        window.decorView.setBackgroundColor(bg())
        window.statusBarColor = bg()
        window.navigationBarColor = bg()
        window.decorView.systemUiVisibility = if (dark) 0 else View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
        for (c in cards) c.background = round(cardBg(), 16)
        for (t in primaryTexts) t.setTextColor(fg())
        for (t in subTexts) t.setTextColor(sub())
        for ((b, sec) in buttons) {
            b.background = round(if (sec) cardBg() else accent, 12)
            b.setTextColor(if (sec) fg() else 0xFFFFFFFF.toInt())
        }
    }

    private fun applyDim() {
        val lp = window.attributes
        lp.screenBrightness = if (prefs.getBoolean("dim", false)) 0.01f else WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
        window.attributes = lp
    }

    private fun applyPreviewSize() {
        val lp = tv.layoutParams as LinearLayout.LayoutParams
        lp.height = if (cfg.optBoolean("preview", false)) dp(200) else dp(2)
        tv.layoutParams = lp
        (previewRow.getChildAt(1) as Switch).isChecked = cfg.optBoolean("preview", false)
    }

    private val tick = object : Runnable {
        override fun run() {
            val now = System.nanoTime()
            val dt = (now - lastT).coerceAtLeast(1L) / 1e9
            lastT = now
            val cap = capCount.get(); val sent = sentCount.get()
            val jn = jpegN.get(); val jns = jpegNs.get(); val dr = dropCount.get()
            val capFps = ((cap - lastCap) / dt).toInt()
            val sentFps = ((sent - lastSent) / dt).toInt()
            val ms = if (jn > lastJn) ((jns - lastJns) / (jn - lastJn)) / 1_000_000L else 0L
            lastCap = cap; lastSent = sent; lastJn = jn; lastJns = jns

            statusTv.text = if (running) "● Yayında" else "○ Durdu"
            statusTv.setTextColor(if (running) 0xFF30D158.toInt() else sub())
            ipTv.text = "WiFi: ${localIps()}:$PORT"
            statsTv.text = "PC: ${if (client != null) "bağlı" else "yok"}   yakalama $capFps fps   " +
                "gönderim $sentFps fps   sıkıştırma $ms ms"
            if (client != null && running) {
                val o = JSONObject().put("cap", capFps).put("sent", sentFps).put("ms", ms).put("drop", dr)
                ctl.execute { sendPacket(12, o.toString().toByteArray()) }
            }
            ui.postDelayed(this, 1000)
        }
    }

    private fun localIps(): String = try {
        NetworkInterface.getNetworkInterfaces().toList().flatMap { it.inetAddresses.toList() }
            .filter { !it.isLoopbackAddress && it is Inet4Address }
            .joinToString { it.hostAddress ?: "" }.ifEmpty { "yok" }
    } catch (e: Exception) { "?" }

    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        prefs = getSharedPreferences("camlink", MODE_PRIVATE)
        dark = prefs.getBoolean("dark", true)
        cfg = defaults()
        try {
            val saved = JSONObject(prefs.getString("cfg", "{}") ?: "{}")
            for (k in saved.keys()) cfg.put(k, saved.get(k))
        } catch (_: Exception) {}
        jqNow = cfg.optInt("jq", 85)
        camThread = HandlerThread("cam").also { it.start() }
        camHandler = Handler(camThread.looper)
        try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "camlink").also { it.acquire() }
        } catch (_: Exception) {}
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        buildUi()
        applyTheme()
        applyDim()
        applyPreviewSize()
        log("Hazır. Ayarlar bilgisayar programından yapılır.")
        startServer()
        ui.post(tick)
        if (prefs.getBoolean("autostart", true)) ui.postDelayed({ ensureStart() }, 700)
    }

    private fun ensureStart() {
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) startAll()
        else requestPermissions(arrayOf(Manifest.permission.CAMERA), 1)
    }

    override fun onRequestPermissionsResult(rc: Int, p: Array<out String>, r: IntArray) {
        if (r.isNotEmpty() && r[0] == PackageManager.PERMISSION_GRANTED) startAll() else log("Kamera izni verilmedi")
    }

    // ---------- ağ ----------
    private fun writeRaw(s: Socket, type: Int, d: ByteArray) {
        val n = d.size
        val buf = ByteArray(n + 5)
        buf[0] = type.toByte()
        buf[1] = (n ushr 24).toByte(); buf[2] = (n ushr 16).toByte()
        buf[3] = (n ushr 8).toByte(); buf[4] = n.toByte()
        System.arraycopy(d, 0, buf, 5, n)
        synchronized(lock) { s.outputStream.write(buf); s.outputStream.flush() }
    }

    private fun sendPacket(type: Int, d: ByteArray) {
        val s = client ?: return
        try { writeRaw(s, type, d) } catch (e: Exception) { dropClient(s) }
    }

    private fun dropClient(s: Socket) {
        try { s.close() } catch (_: Exception) {}
        var was = false
        synchronized(lock) { if (client === s) { client = null; was = true } }
        if (was) log("PC bağlantısı koptu")
    }

    private fun setInfo(text: String) {
        val b = text.toByteArray()
        info = b
        ctl.execute { sendPacket(9, b) }
    }

    private fun buildState(): ByteArray {
        val o = JSONObject()
        o.put("running", running)
        o.put("state", cfg)
        caps?.let { o.put("caps", it) }
        return o.toString().toByteArray(Charsets.UTF_8)
    }

    private fun sendState() {
        if (client == null) return
        ctl.execute { sendPacket(10, buildState()) }
    }

    private fun startServer() = thread(isDaemon = true) {
        try {
            val ss = ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress(PORT)) }
            while (true) {
                val s = ss.accept()
                s.tcpNoDelay = true
                synchronized(lock) {
                    client?.let { try { it.close() } catch (_: Exception) {} }
                    client = null
                }
                try {
                    info?.let { writeRaw(s, 9, it) }
                    writeRaw(s, 10, buildState())
                } catch (e: Exception) { continue }
                client = s
                readCommands(s)
                log("PC bağlandı: ${s.inetAddress.hostAddress}")
            }
        } catch (e: Exception) { log("Sunucu hatası: $e") }
    }

    private fun readCommands(s: Socket) = thread(isDaemon = true) {
        try {
            val din = DataInputStream(s.getInputStream())
            val hdr = ByteArray(5)
            while (true) {
                din.readFully(hdr)
                val n = ((hdr[1].toInt() and 255) shl 24) or ((hdr[2].toInt() and 255) shl 16) or
                    ((hdr[3].toInt() and 255) shl 8) or (hdr[4].toInt() and 255)
                if (n < 0 || n > 65536) break
                val body = ByteArray(n)
                din.readFully(body)
                if (hdr[0].toInt() == 20) {
                    try { handleCmd(JSONObject(String(body, Charsets.UTF_8))) }
                    catch (e: Exception) { log("Komut hatası: $e") }
                }
            }
        } catch (_: Exception) {}
        dropClient(s)
    }

    // PC'den gelen ayar ve komutlar
    private fun handleCmd(j: JSONObject) {
        var restart = false
        var live = false
        val keys = j.keys()
        while (keys.hasNext()) {
            val k = keys.next()
            if (k == "action") continue
            val v = j.get(k)
            if (cfg.opt(k)?.toString() != v.toString()) {
                cfg.put(k, v)
                if (k in RESTART_KEYS) restart = true else live = true
            }
        }
        if (restart || live) saveCfg()
        jqNow = cfg.optInt("jq", 85)
        when (j.optString("action")) {
            "start" -> ui.post { if (!running) ensureStart() }
            "stop" -> ui.post { if (running) stopAll() }
            "af" -> camHandler.post { doAutofocus() }
            "state" -> sendState()
        }
        if (j.has("preview")) ui.post { applyPreviewSize() }
        if (running) {
            if (restart) camHandler.post { stopHw(); startCamera() }
            else if (live) camHandler.post { applyLive() }
        }
    }

    // ---------- başlat / durdur ----------
    private fun startAll() {
        if (running) return
        running = true
        bStart.text = "Durdur"
        camHandler.post { startCamera() }
    }

    private fun stopAll() {
        running = false
        bStart.text = "Başlat"
        camHandler.post { stopHw() }
        ctl.execute { sendPacket(8, ByteArray(0)); sendPacket(10, buildState()) }
        log("Durdu")
    }

    private fun stopHw() {
        gen++
        try { workPool?.shutdownNow() } catch (_: Exception) {}
        workPool = null
        val cam = cam1
        cam1 = null
        if (cam != null) {
            try { cam.setPreviewCallbackWithBuffer(null) } catch (_: Exception) {}
            try { cam.stopPreview() } catch (_: Exception) {}
            try { cam.release() } catch (_: Exception) {}
        }
        try { dummyTex?.release() } catch (_: Exception) {}
        dummyTex = null
        info = null
    }

    private fun abort(msg: String) {
        log(msg)
        ui.post { if (running) stopAll() }
    }

    // ---------- kamera (Camera1 + paralel MJPEG) ----------
    private fun safe(f: () -> Unit) { try { f() } catch (_: Exception) {} }

    private fun applyParams(p: Camera.Parameters) {
        safe { val f = cfg.optString("focus", "continuous-video"); if (p.supportedFocusModes?.contains(f) == true) p.focusMode = f }
        safe { p.exposureCompensation = cfg.optInt("exp", 0).coerceIn(p.minExposureCompensation, p.maxExposureCompensation) }
        safe { if (p.isAutoExposureLockSupported) p.autoExposureLock = cfg.optBoolean("ae_lock", false) }
        safe { if (p.isAutoWhiteBalanceLockSupported) p.autoWhiteBalanceLock = cfg.optBoolean("awb_lock", false) }
        safe { val w = cfg.optString("wb", "auto"); if (p.supportedWhiteBalance?.contains(w) == true) p.whiteBalance = w }
        safe { if (p.isZoomSupported) p.zoom = cfg.optInt("zoom", 0).coerceIn(0, p.maxZoom) }
        safe {
            val t = cfg.optBoolean("torch", false)
            if (p.supportedFlashModes?.contains("torch") == true) p.flashMode = if (t) "torch" else "off"
        }
        safe { val a = cfg.optString("anti", "auto"); if (p.supportedAntibanding?.contains(a) == true) p.antibanding = a }
        safe { if (p.isVideoStabilizationSupported) p.videoStabilization = cfg.optBoolean("stab", false) }
        safe {
            val iso = cfg.optString("iso", "auto")
            val vals = p.get("iso-values")
            if (vals != null) {
                val l = vals.split(",")
                if (iso != "auto" && l.contains(iso)) p.set("iso", iso) else if (l.contains("auto")) p.set("iso", "auto")
            }
        }
    }

    private fun applyLive() {
        val cam = cam1 ?: return
        try {
            val p = cam.parameters
            applyParams(p)
            cam.parameters = p
        } catch (e: Exception) { log("Ayar uygulanamadı: $e") }
    }

    private fun doAutofocus() {
        try {
            cam1?.autoFocus { _, c -> try { c.cancelAutoFocus() } catch (_: Exception) {} }
        } catch (_: Exception) {}
    }

    private fun buildCaps(p: Camera.Parameters): JSONObject {
        val c = JSONObject()
        val sizes = JSONArray()
        p.supportedPreviewSizes.sortedByDescending { it.width * it.height }.forEach { sizes.put("${it.width}x${it.height}") }
        c.put("sizes", sizes)
        val ranges = p.supportedPreviewFpsRange ?: emptyList()
        c.put("fps", JSONArray(listOf(15, 24, 30, 60).filter { v -> ranges.any { it[1] >= v * 1000 } }))
        c.put("focus", JSONArray(p.supportedFocusModes ?: emptyList<String>()))
        c.put("wb", JSONArray(p.supportedWhiteBalance ?: emptyList<String>()))
        c.put("anti", JSONArray(p.supportedAntibanding ?: emptyList<String>()))
        val isoVals = p.get("iso-values")
        c.put("iso", JSONArray(if (isoVals != null) isoVals.split(",") else emptyList<String>()))
        c.put("exp", JSONObject().put("min", p.minExposureCompensation).put("max", p.maxExposureCompensation)
            .put("step", p.exposureCompensationStep.toDouble()))
        if (p.isZoomSupported) c.put("zoom", JSONObject().put("max", p.maxZoom).put("ratios", JSONArray(p.zoomRatios)))
        c.put("ae_lock", p.isAutoExposureLockSupported)
        c.put("awb_lock", p.isAutoWhiteBalanceLockSupported)
        c.put("torch", p.supportedFlashModes?.contains("torch") == true)
        c.put("stab", p.isVideoStabilizationSupported)
        c.put("cams", Camera.getNumberOfCameras())
        return c
    }

    private fun startCamera() {
        val g = gen
        try {
            capCount.set(0); sentCount.set(0); dropCount.set(0)
            lastCap = 0; lastSent = 0; lastJn = 0; lastJns = 0; jpegN.set(0); jpegNs.set(0)

            val want = if (cfg.optInt("cam", 0) == 1) Camera.CameraInfo.CAMERA_FACING_FRONT else Camera.CameraInfo.CAMERA_FACING_BACK
            var idx = 0
            val ci = Camera.CameraInfo()
            for (i in 0 until Camera.getNumberOfCameras()) {
                Camera.getCameraInfo(i, ci)
                if (ci.facing == want) { idx = i; break }
            }
            val cam = Camera.open(idx)
            cam1 = cam
            val p = cam.parameters

            val parts = cfg.optString("res", "1280x720").split("x")
            val ww = parts.getOrNull(0)?.toIntOrNull() ?: 1280
            val hh = parts.getOrNull(1)?.toIntOrNull() ?: 720
            val sz = p.supportedPreviewSizes.minByOrNull { Math.abs(it.width - ww) + Math.abs(it.height - hh) }!!
            p.setPreviewSize(sz.width, sz.height)
            p.previewFormat = ImageFormat.NV21
            try { p.setRecordingHint(true) } catch (_: Exception) {}

            // Sabit fps aralığı: karanlıkta bile kare hızı düşmesin (vfps açıksa düşmesine izin ver)
            val target = cfg.optInt("fps", 30) * 1000
            val ranges = p.supportedPreviewFpsRange ?: emptyList()
            val cand = ranges.filter { it[1] >= target }
            val best = if (cfg.optBoolean("vfps", false))
                cand.sortedWith(compareBy<IntArray> { it[1] }.thenBy { it[0] }).firstOrNull()
            else
                cand.sortedWith(compareBy<IntArray> { it[1] }.thenByDescending { it[0] }).firstOrNull()
            val use = best ?: ranges.maxByOrNull { it[1] }
            if (use != null) p.setPreviewFpsRange(use[0], use[1])

            applyParams(p)
            cam.parameters = p

            val w = sz.width
            val h = sz.height
            val rect = Rect(0, 0, w, h)
            val nWork = 3
            val sem = Semaphore(nWork)
            val pool = Executors.newFixedThreadPool(nWork)
            workPool = pool
            seqCounter = 0L
            turn = 0L
            repeat(nWork + 2) { cam.addCallbackBuffer(ByteArray(w * h * 3 / 2)) }

            cam.setPreviewCallbackWithBuffer { data, cm ->
                capCount.incrementAndGet()
                if (g != gen || !running || client == null || !sem.tryAcquire()) {
                    if (client != null) dropCount.incrementAndGet()
                    cm.addCallbackBuffer(data)
                } else {
                    val my = seqCounter++
                    try {
                        pool.execute {
                            var jpeg: ByteArray? = null
                            try {
                                val t0 = System.nanoTime()
                                val bos = tlBos.get() ?: ByteArrayOutputStream(256 * 1024).also { tlBos.set(it) }
                                bos.reset()
                                YuvImage(data, ImageFormat.NV21, w, h, null).compressToJpeg(rect, jqNow, bos)
                                jpeg = bos.toByteArray()
                                jpegNs.addAndGet(System.nanoTime() - t0)
                                jpegN.incrementAndGet()
                            } catch (_: Exception) {}
                            try { cm.addCallbackBuffer(data) } catch (_: Exception) {}
                            orderLock.lock()
                            try {
                                val dl = System.nanoTime() + 150_000_000L
                                while (g == gen && turn < my && System.nanoTime() < dl) orderCond.await(10, TimeUnit.MILLISECONDS)
                                if (g == gen && running) {
                                    if (jpeg != null) { sendPacket(2, jpeg); sentCount.incrementAndGet() }
                                    if (turn <= my) turn = my + 1
                                    orderCond.signalAll()
                                }
                            } finally {
                                orderLock.unlock()
                                sem.release()
                            }
                        }
                    } catch (_: Exception) {
                        sem.release()
                        try { cm.addCallbackBuffer(data) } catch (_: Exception) {}
                    }
                }
            }

            val previewOn = cfg.optBoolean("preview", false)
            try {
                if (previewOn && tv.surfaceTexture != null) cam.setPreviewTexture(tv.surfaceTexture)
                else { dummyTex = SurfaceTexture(10); cam.setPreviewTexture(dummyTex) }
                cam.startPreview()
            } catch (e: Exception) {
                log("Önizleme açılamadı, gizli önizleme kullanılıyor")
                dummyTex = SurfaceTexture(11)
                cam.setPreviewTexture(dummyTex)
                cam.startPreview()
            }

            val fpsInfo = if (use != null) use[1] / 1000 else 30
            setInfo("mjpeg;$w;$h;$fpsInfo")
            caps = buildCaps(cam.parameters)
            sendState()
            log("Yayın: ${w}x$h, $fpsInfo fps, JPEG $jqNow")

            // Önizleme kapalıyken kare gelmezse önizlemeyi açıp yeniden dene
            camHandler.postDelayed({
                if (running && g == gen && capCount.get() == 0L && !previewOn) {
                    log("Kare gelmedi, önizleme açılıp yeniden deneniyor")
                    cfg.put("preview", true); saveCfg()
                    ui.post { applyPreviewSize() }
                    stopHw(); startCamera()
                }
            }, 3500)
        } catch (e: Exception) { abort("Kamera açılamadı: $e") }
    }

    override fun onDestroy() {
        running = false
        stopHw()
        ui.removeCallbacksAndMessages(null)
        try { wifiLock?.release() } catch (_: Exception) {}
        try { ctl.shutdownNow() } catch (_: Exception) {}
        camThread.quitSafely()
        super.onDestroy()
    }
}
