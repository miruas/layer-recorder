// SPDX-License-Identifier: GPL-3.0-only
//! Persistent native-resolution composition. Effects read GPU textures; only the
//! changed privacy rectangle and its filter halo run through the effect passes.
//! All GL entry points run on eframe's current rendering context/thread.
#![allow(unsafe_op_in_unsafe_fn)]
use eframe::{
    egui::{self, Color32, Rect, Vec2},
    egui_glow,
    glow::{self, HasContext},
};
use std::{
    hash::{Hash, Hasher},
    sync::{Arc, Mutex},
};

#[derive(Clone)]
pub enum Op {
    Mesh(egui::Mesh, Option<Border>),
    Privacy { rect: [f32; 4], kind: Privacy },
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Privacy {
    Black,
    Blur,
    Pixelate,
}
#[derive(Clone, Copy)]
pub struct Border {
    pub width: f32,
    pub size: [f32; 2],
    pub color: Color32,
    pub uv: [f32; 4],
    pub blackout: bool,
}
#[derive(Clone)]
pub struct Frame {
    pub size: [u32; 2],
    pub canvas: [f32; 2],
    pub ops: Vec<Op>,
    pub base_count: usize,
    pub generation: u64,
}
#[derive(Default)]
pub struct Statistics {
    pub frames: u64,
    pub held_frames: u64,
    pub base_renders: u64,
    pub effect_pixels: u64,
    pub last_cpu_ms: f64,
}
pub struct Compositor {
    inner: Mutex<Option<Renderer>>,
    error: Mutex<Option<String>>,
    pub statistics: Mutex<Statistics>,
    capture: Mutex<Option<(std::path::PathBuf, egui::Context)>>,
    capture_result: Arc<Mutex<Option<Result<std::path::PathBuf, String>>>>,
}
impl Compositor {
    pub fn new(gl: &glow::Context) -> Result<Arc<Self>, String> {
        Ok(Arc::new(Self {
            inner: Mutex::new(Some(unsafe { Renderer::new(gl)? })),
            error: Mutex::new(None),
            statistics: Mutex::new(Statistics::default()),
            capture: Mutex::new(None),
            capture_result: Arc::new(Mutex::new(None)),
        }))
    }
    pub fn request_capture(&self, path: std::path::PathBuf, ctx: egui::Context) {
        *self.capture.lock().unwrap() = Some((path, ctx));
    }
    pub fn take_capture(&self) -> Option<Result<std::path::PathBuf, String>> {
        self.capture_result.lock().unwrap().take()
    }
    pub fn take_error(&self) -> Option<String> {
        self.error.lock().unwrap().take()
    }
    pub fn callback(self: &Arc<Self>, rect: Rect, frame: Frame) -> egui::PaintCallback {
        let this = self.clone();
        egui::PaintCallback {
            rect,
            callback: Arc::new(egui_glow::CallbackFn::new(move |info, painter| {
                let started = std::time::Instant::now();
                let mut state = this.inner.lock().unwrap();
                if let Some(renderer) = state.as_mut() {
                    let gl = painter.gl();
                    let result = unsafe { renderer.render(gl, painter, &info, &frame) };
                    // Never leave egui pointing at an offscreen framebuffer.
                    unsafe {
                        gl.bind_framebuffer(glow::FRAMEBUFFER, painter.intermediate_fbo());
                    }
                    match result {
                        Ok(pixels) => {
                            if let Some((path, ctx)) = this.capture.lock().unwrap().take() {
                                let rgba = unsafe { renderer.readback(gl) };
                                let size = renderer.size;
                                let out = this.capture_result.clone();
                                std::thread::spawn(move || {
                                    let result = image::save_buffer(
                                        &path,
                                        &rgba,
                                        size[0],
                                        size[1],
                                        image::ColorType::Rgba8,
                                    )
                                    .map(|_| path)
                                    .map_err(|e| e.to_string());
                                    *out.lock().unwrap() = Some(result);
                                    ctx.request_repaint();
                                });
                            }
                            let mut s = this.statistics.lock().unwrap();
                            s.frames += 1;
                            s.base_renders = renderer.base_renders;
                            s.effect_pixels = pixels;
                            s.last_cpu_ms = started.elapsed().as_secs_f64() * 1000.;
                        }
                        Err(error) => {
                            renderer.cached_canvas = None;
                            *this.error.lock().unwrap() = Some(error);
                        }
                    }
                }
            })),
        }
    }
    pub fn invalidate_cached(&self) {
        if let Some(renderer) = self.inner.lock().unwrap().as_mut() {
            renderer.cached_canvas = None;
        }
    }
    pub fn has_cached(&self, canvas: [f32; 2]) -> bool {
        self.inner
            .lock()
            .unwrap()
            .as_ref()
            .is_some_and(|r| r.cached_canvas == Some(canvas))
    }
    /// Present only the last fully processed frame while a newer sprite/frame
    /// loads. Never consume QA capture requests or present a partial render.
    pub fn cached_callback(self: &Arc<Self>, rect: Rect, canvas: [f32; 2]) -> egui::PaintCallback {
        let this = self.clone();
        egui::PaintCallback {
            rect,
            callback: Arc::new(egui_glow::CallbackFn::new(move |info, painter| {
                let mut state = this.inner.lock().unwrap();
                if let Some(renderer) = state.as_mut()
                    && renderer.cached_canvas == Some(canvas)
                {
                    match unsafe { renderer.present(painter.gl(), painter, &info) } {
                        Ok(()) => this.statistics.lock().unwrap().held_frames += 1,
                        Err(error) => {
                            renderer.cached_canvas = None;
                            *this.error.lock().unwrap() = Some(error);
                        }
                    }
                    unsafe {
                        painter
                            .gl()
                            .bind_framebuffer(glow::FRAMEBUFFER, painter.intermediate_fbo());
                    }
                }
            })),
        }
    }
    pub fn destroy(&self, gl: &glow::Context) {
        if let Some(mut renderer) = self.inner.lock().unwrap().take() {
            unsafe {
                renderer.destroy(gl);
            }
        }
    }
}
struct Target {
    texture: glow::Texture,
    fbo: glow::Framebuffer,
}
struct Renderer {
    mesh: glow::Program,
    effect: glow::Program,
    vao: glow::VertexArray,
    vbo: glow::Buffer,
    ibo: glow::Buffer,
    targets: Vec<Target>,
    size: [u32; 2],
    max_size: u32,
    base_key: Option<u64>,
    base_renders: u64,
    cached_canvas: Option<[f32; 2]>,
}
const MESH_VERTEX: &str = r#"
in vec2 a_pos; in vec2 a_uv; in vec4 a_color;
uniform vec2 u_canvas;
out vec2 v_uv; out vec4 v_color;
void main(){v_uv=a_uv;v_color=a_color;gl_Position=vec4(a_pos.x/u_canvas.x*2.-1.,1.-a_pos.y/u_canvas.y*2.,0.,1.);}
"#;
const MESH_FRAGMENT: &str = r#"
in vec2 v_uv; in vec4 v_color; out vec4 out_color;
uniform sampler2D u_tex;
uniform int u_blackout;uniform vec2 u_border; uniform vec4 u_border_color; uniform vec4 u_uv;
void main(){
 vec4 c=u_blackout==1?vec4(0.,0.,0.,1.):texture(u_tex,v_uv);
 vec2 q=(v_uv-u_uv.xy)/(u_uv.zw-u_uv.xy);
 if(u_border.x>0. && (q.x<u_border.x||q.x>1.-u_border.x||q.y<u_border.y||q.y>1.-u_border.y))c=u_border_color;
 out_color=c*v_color;
}
"#;
const EFFECT_VERTEX: &str = r#"
out vec2 v_uv;
void main(){vec2 p=vec2(float((gl_VertexID<<1)&2),float(gl_VertexID&2));v_uv=p;gl_Position=vec4(p*2.-1.,0.,1.);}
"#;
const EFFECT_FRAGMENT: &str = r#"
in vec2 v_uv; out vec4 out_color;
uniform sampler2D u_tex; uniform vec2 u_size; uniform vec2 u_step; uniform int u_mode; uniform int u_radius;
ivec2 mirror_edge(ivec2 p){ivec2 s=ivec2(u_size);ivec2 period=2*s;p=((p%period)+period)%period;return min(p,period-ivec2(1)-p);}
vec4 pixel(ivec2 p){return texelFetch(u_tex,mirror_edge(p),0);}
void main(){
 if(u_mode==0){out_color=texture(u_tex,v_uv);return;}
 ivec2 p=ivec2(gl_FragCoord.xy);
 if(u_mode==2){
  // Nearest-neighbour down/upscale, anchored to the entire canvas. GL y
  // is bottom-up, so convert to top-down before choosing the grid cell.
  ivec2 size=ivec2(u_size);ivec2 top=ivec2(p.x,size.y-1-p.y);ivec2 grid=max(ivec2(2),size/24);
  // libswscale's nearest path uses rounded16.16 increments, not ideal
  // floating-point pixel centres. Keep its cell phase at every canvas size.
  ivec2 up=(grid*65536+size/2)/size;ivec2 cell=(up/2+top*up)>>16;
  ivec2 down=(size*65536+grid/2)/grid;ivec2 q=(down/2+cell*down)>>16;
  out_color=pixel(ivec2(q.x,size.y-1-q.y));return;
 }
 vec4 sum=vec4(0.);for(int i=-12;i<=12;i++){if(abs(i)<=u_radius)sum+=pixel(p+ivec2(u_step)*i);}
 out_color=sum/float(2*u_radius+1);
}
"#;
unsafe fn program(
    gl: &glow::Context,
    vertex: &str,
    fragment: &str,
) -> Result<glow::Program, String> {
    let prefix = if gl.version().is_embedded {
        "#version 300 es\nprecision highp float;precision highp int;\n"
    } else {
        "#version 140\n"
    };
    let program = gl.create_program()?;
    let mut shaders = vec![];
    for (kind, source) in [
        (glow::VERTEX_SHADER, vertex),
        (glow::FRAGMENT_SHADER, fragment),
    ] {
        let shader = gl.create_shader(kind)?;
        gl.shader_source(shader, &format!("{prefix}{source}"));
        gl.compile_shader(shader);
        if !gl.get_shader_compile_status(shader) {
            let e = gl.get_shader_info_log(shader);
            gl.delete_shader(shader);
            for s in shaders {
                gl.delete_shader(s);
            }
            gl.delete_program(program);
            return Err(format!("GPU preview shader: {e}"));
        }
        gl.attach_shader(program, shader);
        shaders.push(shader);
    }
    gl.link_program(program);
    let ok = gl.get_program_link_status(program);
    let error = gl.get_program_info_log(program);
    for shader in shaders {
        gl.detach_shader(program, shader);
        gl.delete_shader(shader);
    }
    if !ok {
        gl.delete_program(program);
        return Err(format!("GPU preview link: {error}"));
    }
    Ok(program)
}
/// Inclusive FFmpeg privacy mask, converted to a clipped GL pixel rectangle.
pub fn region(rect: [f32; 4], canvas: [f32; 2], size: [u32; 2]) -> Option<[i32; 4]> {
    if rect.iter().chain(canvas.iter()).any(|v| !v.is_finite())
        || canvas.iter().any(|v| *v <= 0.)
        || rect[2] <= 0.
        || rect[3] <= 0.
    {
        return None;
    }
    let sx = size[0] as f32 / canvas[0];
    let sy = size[1] as f32 / canvas[1];
    let x0 = (rect[0] * sx).ceil().max(0.).min(size[0] as f32) as i32;
    let y0 = (rect[1] * sy).ceil().max(0.).min(size[1] as f32) as i32;
    let x1 = (((rect[0] + rect[2]) * sx).floor() + 1.)
        .max(0.)
        .min(size[0] as f32) as i32;
    let y1 = (((rect[1] + rect[3]) * sy).floor() + 1.)
        .max(0.)
        .min(size[1] as f32) as i32;
    (x1 > x0 && y1 > y0).then_some([x0, size[1] as i32 - y1, x1 - x0, y1 - y0])
}
fn expand(r: [i32; 4], dx: i32, dy: i32, size: [u32; 2]) -> [i32; 4] {
    let x = (r[0] - dx).max(0);
    let y = (r[1] - dy).max(0);
    let right = (r[0] + r[2] + dx).min(size[0] as i32);
    let top = (r[1] + r[3] + dy).min(size[1] as i32);
    [x, y, right - x, top - y]
}
fn area(r: [i32; 4]) -> u64 {
    r[2].max(0) as u64 * r[3].max(0) as u64
}
impl Renderer {
    unsafe fn new(gl: &glow::Context) -> Result<Self, String> {
        let mesh = program(gl, MESH_VERTEX, MESH_FRAGMENT)?;
        let effect = match program(gl, EFFECT_VERTEX, EFFECT_FRAGMENT) {
            Ok(p) => p,
            Err(e) => {
                gl.delete_program(mesh);
                return Err(e);
            }
        };
        Ok(Self {
            mesh,
            effect,
            vao: gl.create_vertex_array()?,
            vbo: gl.create_buffer()?,
            ibo: gl.create_buffer()?,
            targets: vec![],
            size: [0, 0],
            base_key: None,
            base_renders: 0,
            cached_canvas: None,
            max_size: gl.get_parameter_i32(glow::MAX_TEXTURE_SIZE).max(0) as u32,
        })
    }
    unsafe fn targets(&mut self, gl: &glow::Context, size: [u32; 2]) -> Result<(), String> {
        if size == self.size {
            return Ok(());
        }
        if size[0] < 2
            || size[1] < 2
            || size[0] > 3840
            || size[1] > 2160
            || size.iter().any(|n| *n > self.max_size)
        {
            return Err(
                "Preview exceeds the GPU texture limit; choose a smaller preview size.".into(),
            );
        }
        for target in self.targets.drain(..) {
            gl.delete_framebuffer(target.fbo);
            gl.delete_texture(target.texture);
        }
        self.size = [0, 0];
        self.base_key = None;
        for _ in 0..4 {
            let texture = gl.create_texture()?;
            let fbo = gl.create_framebuffer()?;
            gl.bind_texture(glow::TEXTURE_2D, Some(texture));
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MIN_FILTER,
                glow::LINEAR as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MAG_FILTER,
                glow::LINEAR as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_WRAP_S,
                glow::CLAMP_TO_EDGE as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_WRAP_T,
                glow::CLAMP_TO_EDGE as i32,
            );
            gl.tex_image_2d(
                glow::TEXTURE_2D,
                0,
                glow::RGBA8 as i32,
                size[0] as i32,
                size[1] as i32,
                0,
                glow::RGBA,
                glow::UNSIGNED_BYTE,
                glow::PixelUnpackData::Slice(None),
            );
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            gl.framebuffer_texture_2d(
                glow::FRAMEBUFFER,
                glow::COLOR_ATTACHMENT0,
                glow::TEXTURE_2D,
                Some(texture),
                0,
            );
            if gl.check_framebuffer_status(glow::FRAMEBUFFER) != glow::FRAMEBUFFER_COMPLETE {
                gl.delete_framebuffer(fbo);
                gl.delete_texture(texture);
                return Err("GPU preview framebuffer is incomplete.".into());
            }
            self.targets.push(Target { texture, fbo });
        }
        self.size = size;
        Ok(())
    }
    unsafe fn mesh(
        &self,
        gl: &glow::Context,
        painter: &egui_glow::Painter,
        mesh: &egui::Mesh,
        border: Option<Border>,
        canvas: [f32; 2],
    ) -> Result<(), String> {
        if mesh.indices.is_empty() {
            return Ok(());
        }
        let texture = painter
            .texture(mesh.texture_id)
            .ok_or("Preview source texture is unavailable.")?;
        gl.use_program(Some(self.mesh));
        gl.uniform_2_f32(
            gl.get_uniform_location(self.mesh, "u_canvas").as_ref(),
            canvas[0],
            canvas[1],
        );
        gl.uniform_1_i32(gl.get_uniform_location(self.mesh, "u_tex").as_ref(), 0);
        gl.active_texture(glow::TEXTURE0);
        gl.bind_texture(glow::TEXTURE_2D, Some(texture));
        let border = border.unwrap_or(Border {
            width: 0.,
            size: [1., 1.],
            color: Color32::BLACK,
            uv: [0., 0., 1., 1.],
            blackout: false,
        });
        gl.uniform_1_i32(
            gl.get_uniform_location(self.mesh, "u_blackout").as_ref(),
            if border.blackout { 1 } else { 0 },
        );
        gl.uniform_2_f32(
            gl.get_uniform_location(self.mesh, "u_border").as_ref(),
            border.width / border.size[0].max(1.),
            border.width / border.size[1].max(1.),
        );
        let c = border.color.to_srgba_unmultiplied();
        gl.uniform_4_f32(
            gl.get_uniform_location(self.mesh, "u_border_color")
                .as_ref(),
            c[0] as f32 / 255.,
            c[1] as f32 / 255.,
            c[2] as f32 / 255.,
            1.,
        );
        gl.uniform_4_f32_slice(
            gl.get_uniform_location(self.mesh, "u_uv").as_ref(),
            &border.uv,
        );
        let mut vertices = Vec::<f32>::with_capacity(mesh.vertices.len() * 8);
        for v in &mesh.vertices {
            vertices.extend([v.pos.x, v.pos.y, v.uv.x, v.uv.y]);
            vertices.extend(v.color.to_array().map(|n| n as f32 / 255.));
        }
        gl.bind_vertex_array(Some(self.vao));
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(self.vbo));
        gl.bind_buffer(glow::ELEMENT_ARRAY_BUFFER, Some(self.ibo));
        gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes(&vertices), glow::STREAM_DRAW);
        gl.buffer_data_u8_slice(
            glow::ELEMENT_ARRAY_BUFFER,
            bytes(&mesh.indices),
            glow::STREAM_DRAW,
        );
        for (name, count, offset) in [("a_pos", 2, 0), ("a_uv", 2, 8), ("a_color", 4, 16)] {
            if let Some(a) = gl.get_attrib_location(self.mesh, name) {
                gl.enable_vertex_attrib_array(a);
                gl.vertex_attrib_pointer_f32(a, count, glow::FLOAT, false, 32, offset);
            }
        }
        gl.draw_elements(
            glow::TRIANGLES,
            mesh.indices.len() as i32,
            glow::UNSIGNED_INT,
            0,
        );
        Ok(())
    }
    unsafe fn pass(
        &self,
        gl: &glow::Context,
        source: usize,
        target: usize,
        rect: [i32; 4],
        mode: i32,
        step: [f32; 2],
    ) {
        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(self.targets[target].fbo));
        gl.viewport(0, 0, self.size[0] as i32, self.size[1] as i32);
        gl.enable(glow::SCISSOR_TEST);
        gl.scissor(rect[0], rect[1], rect[2], rect[3]);
        gl.disable(glow::BLEND);
        gl.use_program(Some(self.effect));
        gl.bind_vertex_array(Some(self.vao));
        gl.active_texture(glow::TEXTURE0);
        gl.bind_texture(glow::TEXTURE_2D, Some(self.targets[source].texture));
        gl.uniform_1_i32(gl.get_uniform_location(self.effect, "u_tex").as_ref(), 0);
        gl.uniform_1_i32(
            gl.get_uniform_location(self.effect, "u_mode").as_ref(),
            mode,
        );
        gl.uniform_1_i32(
            gl.get_uniform_location(self.effect, "u_radius").as_ref(),
            12,
        );
        gl.uniform_2_f32(
            gl.get_uniform_location(self.effect, "u_size").as_ref(),
            self.size[0] as f32,
            self.size[1] as f32,
        );
        gl.uniform_2_f32(
            gl.get_uniform_location(self.effect, "u_step").as_ref(),
            step[0],
            step[1],
        );
        gl.draw_arrays(glow::TRIANGLES, 0, 3);
    }
    unsafe fn render(
        &mut self,
        gl: &glow::Context,
        painter: &egui_glow::Painter,
        info: &egui::PaintCallbackInfo,
        frame: &Frame,
    ) -> Result<u64, String> {
        self.cached_canvas = None;
        self.targets(gl, frame.size)?;
        gl.disable(glow::DEPTH_TEST);
        gl.disable(glow::CULL_FACE);
        gl.disable(glow::SCISSOR_TEST);
        let key = base_key(frame);
        if self.base_key != Some(key) {
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(self.targets[3].fbo));
            gl.viewport(0, 0, self.size[0] as i32, self.size[1] as i32);
            gl.clear_color(0., 0., 0., 1.);
            gl.clear(glow::COLOR_BUFFER_BIT);
            gl.enable(glow::BLEND);
            gl.blend_equation(glow::FUNC_ADD);
            gl.blend_func_separate(
                glow::ONE,
                glow::ONE_MINUS_SRC_ALPHA,
                glow::ONE_MINUS_DST_ALPHA,
                glow::ONE,
            );
            for op in frame.ops.iter().take(frame.base_count) {
                if let Op::Mesh(mesh, border) = op {
                    self.mesh(gl, painter, mesh, *border, frame.canvas)?;
                }
            }
            self.base_key = Some(key);
            self.base_renders += 1;
        }
        // Restore the cached underlay by a GPU copy. Moving an effect changes
        // uniforms and ROI passes, without redrawing or decoding its sources.
        gl.bind_framebuffer(glow::READ_FRAMEBUFFER, Some(self.targets[3].fbo));
        gl.bind_framebuffer(glow::DRAW_FRAMEBUFFER, Some(self.targets[0].fbo));
        gl.blit_framebuffer(
            0,
            0,
            self.size[0] as i32,
            self.size[1] as i32,
            0,
            0,
            self.size[0] as i32,
            self.size[1] as i32,
            glow::COLOR_BUFFER_BIT,
            glow::NEAREST,
        );
        let mut pixels = 0;
        for op in frame.ops.iter().skip(frame.base_count) {
            match op {
                Op::Mesh(mesh, border) => {
                    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(self.targets[0].fbo));
                    gl.viewport(0, 0, self.size[0] as i32, self.size[1] as i32);
                    gl.disable(glow::SCISSOR_TEST);
                    gl.enable(glow::BLEND);
                    gl.blend_equation(glow::FUNC_ADD);
                    gl.blend_func_separate(
                        glow::ONE,
                        glow::ONE_MINUS_SRC_ALPHA,
                        glow::ONE_MINUS_DST_ALPHA,
                        glow::ONE,
                    );
                    self.mesh(gl, painter, mesh, *border, frame.canvas)?;
                }
                Op::Privacy { rect, kind } => {
                    if let Some(r) = region(*rect, frame.canvas, self.size) {
                        match kind {
                            Privacy::Black => {
                                gl.bind_framebuffer(glow::FRAMEBUFFER, Some(self.targets[0].fbo));
                                gl.enable(glow::SCISSOR_TEST);
                                gl.scissor(r[0], r[1], r[2], r[3]);
                                gl.clear_color(0., 0., 0., 1.);
                                gl.clear(glow::COLOR_BUFFER_BIT);
                                pixels += area(r);
                            }
                            Privacy::Pixelate => {
                                self.pass(gl, 0, 1, r, 2, [0., 0.]);
                                gl.bind_framebuffer(
                                    glow::READ_FRAMEBUFFER,
                                    Some(self.targets[1].fbo),
                                );
                                gl.bind_framebuffer(
                                    glow::DRAW_FRAMEBUFFER,
                                    Some(self.targets[0].fbo),
                                );
                                gl.blit_framebuffer(
                                    r[0],
                                    r[1],
                                    r[0] + r[2],
                                    r[1] + r[3],
                                    r[0],
                                    r[1],
                                    r[0] + r[2],
                                    r[1] + r[3],
                                    glow::COLOR_BUFFER_BIT,
                                    glow::NEAREST,
                                );
                                pixels += area(r);
                            }
                            Privacy::Blur => {
                                let a = expand(r, 12, 24, self.size);
                                let b = expand(r, 12, 12, self.size);
                                let c = expand(r, 0, 12, self.size);
                                self.pass(gl, 0, 1, a, 1, [1., 0.]);
                                self.pass(gl, 1, 2, b, 1, [0., 1.]);
                                self.pass(gl, 2, 1, c, 1, [1., 0.]);
                                self.pass(gl, 1, 0, r, 1, [0., 1.]);
                                pixels += area(a) + area(b) + area(c) + area(r);
                            }
                        }
                    }
                }
            }
        }
        let pending_error = gl.get_error();
        if pending_error != glow::NO_ERROR {
            return Err(format!(
                "GPU effect failed {pending_error:#x}; using accurate compositor."
            ));
        }
        self.present(gl, painter, info)?;
        self.cached_canvas = Some(frame.canvas);
        Ok(pixels)
    }
    unsafe fn present(
        &self,
        gl: &glow::Context,
        painter: &egui_glow::Painter,
        info: &egui::PaintCallbackInfo,
    ) -> Result<(), String> {
        gl.bind_framebuffer(glow::FRAMEBUFFER, painter.intermediate_fbo());
        let v = info.viewport_in_pixels();
        let clip = info.clip_rect_in_pixels();
        gl.viewport(v.left_px, v.from_bottom_px, v.width_px, v.height_px);
        gl.enable(glow::SCISSOR_TEST);
        gl.scissor(
            clip.left_px,
            clip.from_bottom_px,
            clip.width_px,
            clip.height_px,
        );
        gl.disable(glow::BLEND);
        gl.use_program(Some(self.effect));
        gl.bind_vertex_array(Some(self.vao));
        gl.active_texture(glow::TEXTURE0);
        gl.bind_texture(glow::TEXTURE_2D, Some(self.targets[0].texture));
        gl.uniform_1_i32(gl.get_uniform_location(self.effect, "u_tex").as_ref(), 0);
        gl.uniform_1_i32(gl.get_uniform_location(self.effect, "u_mode").as_ref(), 0);
        gl.draw_arrays(glow::TRIANGLES, 0, 3);
        let error = gl.get_error();
        if error != glow::NO_ERROR {
            return Err(format!(
                "GPU preview error {error:#x}; using accurate compositor."
            ));
        }
        Ok(())
    }
    unsafe fn readback(&self, gl: &glow::Context) -> Vec<u8> {
        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(self.targets[0].fbo));
        gl.pixel_store_i32(glow::PACK_ALIGNMENT, 1);
        let stride = self.size[0] as usize * 4;
        let mut data = vec![0; stride * self.size[1] as usize];
        gl.read_pixels(
            0,
            0,
            self.size[0] as i32,
            self.size[1] as i32,
            glow::RGBA,
            glow::UNSIGNED_BYTE,
            glow::PixelPackData::Slice(Some(&mut data)),
        );
        for y in 0..self.size[1] as usize / 2 {
            let opposite = self.size[1] as usize - 1 - y;
            for x in 0..stride {
                data.swap(y * stride + x, opposite * stride + x);
            }
        }
        data
    }
    unsafe fn destroy(&mut self, gl: &glow::Context) {
        for t in self.targets.drain(..) {
            gl.delete_texture(t.texture);
            gl.delete_framebuffer(t.fbo);
        }
        gl.delete_program(self.mesh);
        gl.delete_program(self.effect);
        gl.delete_buffer(self.vbo);
        gl.delete_buffer(self.ibo);
        gl.delete_vertex_array(self.vao);
    }
}
fn base_key(frame: &Frame) -> u64 {
    let mut h = std::collections::hash_map::DefaultHasher::new();
    frame.generation.hash(&mut h);
    frame.size.hash(&mut h);
    frame.canvas.map(f32::to_bits).hash(&mut h);
    for op in frame.ops.iter().take(frame.base_count) {
        if let Op::Mesh(mesh, border) = op {
            mesh.texture_id.hash(&mut h);
            mesh.indices.hash(&mut h);
            for v in &mesh.vertices {
                [v.pos.x, v.pos.y, v.uv.x, v.uv.y]
                    .map(f32::to_bits)
                    .hash(&mut h);
                v.color.to_array().hash(&mut h);
            }
            if let Some(b) = border {
                b.width.to_bits().hash(&mut h);
                b.size.map(f32::to_bits).hash(&mut h);
                b.color.to_array().hash(&mut h);
                b.uv.map(f32::to_bits).hash(&mut h);
                b.blackout.hash(&mut h);
            }
        }
    }
    h.finish()
}
fn bytes<T>(items: &[T]) -> &[u8] {
    unsafe { std::slice::from_raw_parts(items.as_ptr().cast(), std::mem::size_of_val(items)) }
}
/// Tessellate cached vector overlays once per changed scene, in native canvas
/// coordinates. The caller retains the frame across idle GUI redraws.
pub fn vector_ops(ctx: &egui::Context, shapes: Vec<egui::Shape>, canvas: [f32; 2]) -> Vec<Op> {
    ctx.tessellate(
        shapes
            .into_iter()
            .map(|shape| egui::epaint::ClippedShape {
                clip_rect: Rect::from_min_size(egui::Pos2::ZERO, Vec2::new(canvas[0], canvas[1])),
                shape,
            })
            .collect(),
        1.,
    )
    .into_iter()
    .filter_map(|p| match p.primitive {
        egui::epaint::Primitive::Mesh(mesh) => Some(Op::Mesh(mesh, None)),
        _ => None,
    })
    .collect()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn privacy_regions_are_inclusive_clipped_and_bottom_up() {
        assert_eq!(
            region([10., 20., 30., 40.], [100., 100.], [100, 100]),
            Some([10, 39, 31, 41])
        );
        assert_eq!(
            region([-5., -5., 10., 10.], [100., 100.], [100, 100]),
            Some([0, 94, 6, 6])
        );
        assert_eq!(region([105., 0., 5., 5.], [100., 100.], [100, 100]), None);
        assert_eq!(
            region([f32::NAN, 0., 5., 5.], [100., 100.], [100, 100]),
            None
        );
    }
    #[test]
    fn cached_underlay_key_ignores_effect_only_edits_but_not_uploads() {
        let mut mesh = egui::Mesh::with_texture(egui::TextureId::Managed(4));
        mesh.add_rect_with_uv(
            Rect::from_min_size(egui::Pos2::ZERO, Vec2::new(1920., 1080.)),
            Rect::from_min_max(egui::Pos2::ZERO, egui::Pos2::new(1., 1.)),
            Color32::WHITE,
        );
        let mut frame = Frame {
            size: [1920, 1080],
            canvas: [1920., 1080.],
            ops: vec![
                Op::Mesh(mesh, None),
                Op::Privacy {
                    rect: [10., 10., 100., 100.],
                    kind: Privacy::Blur,
                },
            ],
            base_count: 1,
            generation: 1,
        };
        let key = base_key(&frame);
        frame.ops[1] = Op::Privacy {
            rect: [500., 400., 100., 100.],
            kind: Privacy::Pixelate,
        };
        assert_eq!(base_key(&frame), key);
        frame.generation += 1;
        assert_ne!(base_key(&frame), key);
    }
    #[test]
    fn blur_work_is_bounded_to_roi_and_required_halo() {
        let r = region([100., 200., 80., 60.], [1920., 1080.], [1920, 1080]).unwrap();
        let work = area(expand(r, 12, 24, [1920, 1080]))
            + area(expand(r, 12, 12, [1920, 1080]))
            + area(expand(r, 0, 12, [1920, 1080]))
            + area(r);
        assert!(work < 1920 * 1080 / 20);
        assert_eq!(expand([0, 0, 10, 10], 24, 24, [20, 20]), [0, 0, 20, 20]);
    }
}
