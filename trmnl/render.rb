# Rendu local des gabarits avec le moteur Liquid, pour aperçu dans un navigateur.
require "liquid"
require "json"
dir, out = ARGV[0], ARGV[1]
now = ARGV[2] ? ARGV[2].to_i : Time.now.to_i
module NowFilter
  def date(input, fmt)
    input = NOW if input == "now"
    t = input.is_a?(Integer) || input.to_s =~ /\A\d+\z/ ? Time.at(input.to_i).utc : input
    super(t, fmt)
  end
end
NOW = now
Liquid::Template.register_filter(NowFilter)
html = []
ARGV[3..].each do |json|
  data = JSON.parse(File.read(json))
  trmnl = { "plugin_settings" => { "custom_fields_values" => {} }, "user" => { "utc_offset" => 7200 } }
  html << "<h2 style='font:16px sans-serif;margin:24px 0 4px'>#{File.basename(json)}</h2>"
  [["full", 800, 480], ["quadrant", 400, 240], ["half_vertical", 400, 480]].each do |view, w, h|
    tpl = Liquid::Template.parse(File.read(File.join(dir, "#{view}.liquid")), error_mode: :strict)
    r = tpl.render!(data.merge("trmnl" => trmnl))
    html << "<div style='display:inline-block;vertical-align:top;margin:0 12px 12px 0'><div style='font:12px sans-serif'>#{view}</div><div class='screen' style='width:#{w}px;height:#{h}px;border:1px solid #999'><div class='view view--#{view}'>#{r}</div></div></div>"
  end
end
File.write(out, "<!doctype html><meta charset='utf-8'><link rel='stylesheet' href='https://trmnl.com/css/latest/plugins.css'><script src='https://trmnl.com/js/latest/plugins.js'></script><body class='environment trmnl' style='padding:16px;background:#fff'>" + html.join + "</body>")
puts "ok"
