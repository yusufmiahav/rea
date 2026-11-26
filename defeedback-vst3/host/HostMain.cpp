// REA Plugin Hub: a small VST3 host. Desk channels come in over ASIO, run
// through a routing graph of plugin nodes (De-Feedback AI is built in), and go
// back out. Right-click the canvas to add nodes, drag from an output pin to an
// input pin to connect, double-click a node to open its window.
#include <juce_audio_utils/juce_audio_utils.h>
#include <juce_gui_extra/juce_gui_extra.h>

#include "../source/PluginEditor.h"
#include "../source/PluginProcessor.h"

namespace hub {

using Graph = juce::AudioProcessorGraph;
using NodeID = Graph::NodeID;
using IOProc = Graph::AudioGraphIOProcessor;

namespace col {
const juce::Colour bg{0xff101318}, grid{0xff1a1f26}, node{0xff1f252e}, nodeHi{0xff2a323d}, edge{0xff3a4552},
    text{0xffd9e1ea}, dim{0xff7d8a99}, accent{0xff2ec4b6}, wire{0xff2ec4b6}, io{0xff9b8cff}, dfb{0xffffb547}, off{0xffff5d5d};
}

static const char* kInternalDfb = "internal:defeedback-ai";

// ------------------------------------------------------------------ plugin window
class PluginWindow : public juce::DocumentWindow {
public:
    PluginWindow(Graph::Node::Ptr n, std::function<void(NodeID)> onClose)
        : DocumentWindow(n->getProcessor()->getName(), col::bg, DocumentWindow::closeButton | DocumentWindow::minimiseButton),
          node(n), closed(std::move(onClose)) {
        setUsingNativeTitleBar(true);
        auto* p = n->getProcessor();
        juce::AudioProcessorEditor* ed = p->hasEditor() ? p->createEditorIfNeeded() : nullptr;
        if (ed == nullptr) ed = new juce::GenericAudioProcessorEditor(*p);
        setContentOwned(ed, true);
        setResizable(ed->isResizable(), false);
        centreWithSize(getWidth(), getHeight());
        setVisible(true);
    }
    ~PluginWindow() override { clearContentComponent(); }
    void closeButtonPressed() override { closed(node->nodeID); }
    Graph::Node::Ptr node;
private:
    std::function<void(NodeID)> closed;
};

// ------------------------------------------------------------------ graph canvas
class Canvas : public juce::Component, private juce::ChangeListener, private juce::Timer {
public:
    struct Host {
        virtual ~Host() = default;
        virtual void showAddMenu(juce::Point<int> where) = 0;
        virtual void openWindow(NodeID) = 0;
        virtual void deleteNode(NodeID) = 0;
        virtual juce::String channelName(bool input, int ch) = 0;
    };

    Canvas(Graph& g, Host& h) : graph(g), host(h) {
        graph.addChangeListener(this);
        setSize(3200, 2000);
        startTimerHz(10);
    }
    ~Canvas() override { graph.removeChangeListener(this); }

    static constexpr int kW = 190, kHead = 26, kPin = 16;

    juce::Rectangle<int> nodeBounds(Graph::Node* n) const {
        auto* p = n->getProcessor();
        const int rows = juce::jmax(1, p->getTotalNumInputChannels(), p->getTotalNumOutputChannels());
        const int x = (int)n->properties.getWithDefault("x", 100), y = (int)n->properties.getWithDefault("y", 100);
        return {x, y, kW, kHead + rows * kPin + 8};
    }
    juce::Point<float> pinPos(Graph::Node* n, bool input, int ch) const {
        auto b = nodeBounds(n).toFloat();
        return {input ? b.getX() : b.getRight(), b.getY() + kHead + ch * kPin + kPin * 0.5f + 2};
    }

    void paint(juce::Graphics& g) override {
        g.fillAll(col::bg);
        g.setColour(col::grid);
        for (int x = 0; x < getWidth(); x += 40) g.drawVerticalLine(x, 0.f, (float)getHeight());
        for (int y = 0; y < getHeight(); y += 40) g.drawHorizontalLine(y, 0.f, (float)getWidth());

        for (auto& c : graph.getConnections()) {
            auto* s = graph.getNodeForId(c.source.nodeID); auto* d = graph.getNodeForId(c.destination.nodeID);
            if (!s || !d || c.source.isMIDI()) continue;
            g.setColour(col::wire.withAlpha(s->isBypassed() ? 0.35f : 0.85f));
            g.strokePath(wirePath(pinPos(s, false, c.source.channelIndex), pinPos(d, true, c.destination.channelIndex)), juce::PathStrokeType(2.2f));
        }
        if (dragging) {
            g.setColour(col::text.withAlpha(0.8f));
            const auto a = dragFromInput ? dragEnd : pinPos(graph.getNodeForId(dragNode), false, dragCh);
            const auto b = dragFromInput ? pinPos(graph.getNodeForId(dragNode), true, dragCh) : dragEnd;
            g.strokePath(wirePath(a, b), juce::PathStrokeType(2.f));
        }
        for (auto* n : graph.getNodes()) paintNode(g, n);
        if (graph.getNumNodes() <= 2) {
            g.setColour(col::dim);
            g.setFont(juce::FontOptions(15.f));
            g.drawText("Right-click to add De-Feedback AI or a plugin. Drag from an output pin (right) to an input pin (left) to connect.",
                       40, 20, 1200, 24, juce::Justification::left);
        }
    }

    void mouseDown(const juce::MouseEvent& e) override {
        const auto p = e.position;
        if (auto hit = pinAt(p)) {
            if (e.mods.isPopupMenu()) { pinMenu(*hit); return; }
            dragging = true; dragNode = hit->node; dragCh = hit->ch; dragFromInput = hit->input; dragEnd = p;
            return;
        }
        if (auto* n = nodeAt(p)) {
            if (e.mods.isPopupMenu()) { nodeMenu(n); return; }
            moving = n->nodeID;
            grabOffset = p - nodeBounds(n).getPosition().toFloat();
            return;
        }
        if (e.mods.isPopupMenu()) {
            if (auto c = connectionAt(p)) {
                juce::PopupMenu m;
                m.addItem(1, "Delete connection");
                m.showMenuAsync({}, [this, conn = *c](int r) { if (r == 1) graph.removeConnection(conn); });
                return;
            }
            host.showAddMenu(p.toInt());
        }
    }

    void mouseDrag(const juce::MouseEvent& e) override {
        if (dragging) { dragEnd = e.position; repaint(); return; }
        if (auto* n = graph.getNodeForId(moving)) {
            const auto pos = (e.position - grabOffset).toInt();
            n->properties.set("x", juce::jmax(0, pos.x));
            n->properties.set("y", juce::jmax(0, pos.y));
            repaint();
        }
    }

    void mouseUp(const juce::MouseEvent& e) override {
        if (dragging) {
            dragging = false;
            if (auto hit = pinAt(e.position); hit && hit->input != dragFromInput) {
                Graph::Connection c = dragFromInput
                    ? Graph::Connection{{hit->node, hit->ch}, {dragNode, dragCh}}
                    : Graph::Connection{{dragNode, dragCh}, {hit->node, hit->ch}};
                graph.addConnection(c);
            }
            repaint();
        }
        moving = {};
    }

    void mouseDoubleClick(const juce::MouseEvent& e) override {
        if (auto* n = nodeAt(e.position)) host.openWindow(n->nodeID);
    }

private:
    struct Pin { NodeID node; int ch; bool input; };

    static juce::Path wirePath(juce::Point<float> a, juce::Point<float> b) {
        juce::Path p;
        p.startNewSubPath(a);
        const float dx = juce::jmax(40.f, std::abs(b.x - a.x) * 0.5f);
        p.cubicTo(a.x + dx, a.y, b.x - dx, b.y, b.x, b.y);
        return p;
    }

    juce::Colour accentFor(Graph::Node* n) const {
        if (dynamic_cast<IOProc*>(n->getProcessor())) return col::io;
        if (dynamic_cast<DeFeedbackProcessor*>(n->getProcessor())) return col::dfb;
        return col::accent;
    }

    void paintNode(juce::Graphics& g, Graph::Node* n) {
        auto* p = n->getProcessor();
        auto b = nodeBounds(n).toFloat();
        const auto acc = accentFor(n);
        g.setColour(juce::Colours::black.withAlpha(0.35f));
        g.fillRoundedRectangle(b.translated(3, 4), 7.f);
        g.setColour(col::node);
        g.fillRoundedRectangle(b, 7.f);
        g.setColour(n->isBypassed() ? col::off : acc.withAlpha(0.9f));
        g.drawRoundedRectangle(b, 7.f, 1.5f);
        auto head = b.removeFromTop((float)kHead);
        g.setColour(acc.withAlpha(0.16f));
        g.fillRoundedRectangle(head.reduced(1.5f), 6.f);
        g.setColour(col::text);
        g.setFont(juce::FontOptions(13.f, juce::Font::bold));
        juce::String title = p->getName();
        if (auto* io = dynamic_cast<IOProc*>(p)) title = io->isInput() ? "DESK IN (audio device)" : "DESK OUT (audio device)";
        if (n->isBypassed()) title += "  [bypassed]";
        g.drawText(title, head.reduced(10, 0), juce::Justification::centredLeft, true);

        g.setFont(juce::FontOptions(10.5f));
        auto* io = dynamic_cast<IOProc*>(p);
        for (int i = 0; i < p->getTotalNumInputChannels(); ++i) {
            const auto pt = pinPos(n, true, i);
            g.setColour(col::edge); g.fillEllipse(pt.x - 5, pt.y - 5, 10, 10);
            g.setColour(acc); g.drawEllipse(pt.x - 5, pt.y - 5, 10, 10, 1.3f);
            g.setColour(col::dim);
            const auto name = io ? host.channelName(false, i) : juce::String(i + 1);
            g.drawText(name, (int)pt.x + 9, (int)pt.y - 7, kW / 2 - 12, 14, juce::Justification::left, true);
        }
        for (int i = 0; i < p->getTotalNumOutputChannels(); ++i) {
            const auto pt = pinPos(n, false, i);
            g.setColour(col::edge); g.fillEllipse(pt.x - 5, pt.y - 5, 10, 10);
            g.setColour(acc); g.drawEllipse(pt.x - 5, pt.y - 5, 10, 10, 1.3f);
            g.setColour(col::dim);
            const auto name = io ? host.channelName(true, i) : juce::String(i + 1);
            g.drawText(name, (int)pt.x - kW / 2 + 3, (int)pt.y - 7, kW / 2 - 12, 14, juce::Justification::right, true);
        }
    }

    Graph::Node* nodeAt(juce::Point<float> p) const {
        auto nodes = graph.getNodes();
        for (int i = nodes.size(); --i >= 0;)
            if (nodeBounds(nodes[i]).toFloat().contains(p)) return nodes[i];
        return nullptr;
    }

    std::optional<Pin> pinAt(juce::Point<float> p) const {
        for (auto* n : graph.getNodes()) {
            auto* pr = n->getProcessor();
            for (int i = 0; i < pr->getTotalNumInputChannels(); ++i)
                if (pinPos(n, true, i).getDistanceFrom(p) < 9) return Pin{n->nodeID, i, true};
            for (int i = 0; i < pr->getTotalNumOutputChannels(); ++i)
                if (pinPos(n, false, i).getDistanceFrom(p) < 9) return Pin{n->nodeID, i, false};
        }
        return std::nullopt;
    }

    std::optional<Graph::Connection> connectionAt(juce::Point<float> p) const {
        for (auto& c : graph.getConnections()) {
            auto* s = graph.getNodeForId(c.source.nodeID); auto* d = graph.getNodeForId(c.destination.nodeID);
            if (!s || !d || c.source.isMIDI()) continue;
            auto path = wirePath(pinPos(s, false, c.source.channelIndex), pinPos(d, true, c.destination.channelIndex));
            juce::Point<float> nearest;
            path.getNearestPoint(p, nearest);
            if (nearest.getDistanceFrom(p) < 6) return c;
        }
        return std::nullopt;
    }

    void pinMenu(const Pin& pin) {
        juce::PopupMenu m;
        m.addItem(1, "Disconnect this pin");
        m.showMenuAsync({}, [this, pin](int r) {
            if (r != 1) return;
            for (auto& c : graph.getConnections())
                if ((pin.input && c.destination.nodeID == pin.node && c.destination.channelIndex == pin.ch) ||
                    (!pin.input && c.source.nodeID == pin.node && c.source.channelIndex == pin.ch))
                    graph.removeConnection(c);
        });
    }

    void nodeMenu(Graph::Node* n) {
        const bool isIO = dynamic_cast<IOProc*>(n->getProcessor()) != nullptr;
        juce::PopupMenu m;
        if (!isIO) {
            m.addItem(1, "Open window");
            m.addItem(2, "Bypass", true, n->isBypassed());
        }
        m.addItem(3, "Disconnect all");
        if (!isIO) { m.addSeparator(); m.addItem(4, "Delete node"); }
        m.showMenuAsync({}, [this, id = n->nodeID](int r) {
            auto* node = graph.getNodeForId(id);
            if (!node) return;
            if (r == 1) host.openWindow(id);
            if (r == 2) { node->setBypassed(!node->isBypassed()); repaint(); }
            if (r == 3) graph.disconnectNode(id);
            if (r == 4) host.deleteNode(id);
        });
    }

    void changeListenerCallback(juce::ChangeBroadcaster*) override { repaint(); }
    void timerCallback() override { repaint(); } // IO channel counts follow the audio device

    Graph& graph;
    Host& host;
    bool dragging = false, dragFromInput = false;
    NodeID dragNode, moving;
    int dragCh = 0;
    juce::Point<float> dragEnd, grabOffset;
};

// ------------------------------------------------------------------ main component
class Main : public juce::Component, public Canvas::Host, private juce::ChangeListener, private juce::Timer {
public:
    Main() {
        juce::PropertiesFile::Options o;
        o.applicationName = "REA Plugin Hub";
        o.folderName = "REA Plugin Hub";
        o.filenameSuffix = "settings";
        o.osxLibrarySubFolder = "Application Support";
        props.setStorageParameters(o);

        formats.addDefaultFormats();
        if (auto xml = props.getUserSettings()->getXmlValue("pluginList")) knownPlugins.recreateFromXml(*xml);
        knownPlugins.addChangeListener(this);

        auto devState = props.getUserSettings()->getXmlValue("audioDevice");
        deviceManager.initialise(256, 256, devState.get(), true);
        deviceManager.addChangeListener(this);
        player.setProcessor(&graph);
        deviceManager.addAudioCallback(&player);

        canvas = std::make_unique<Canvas>(graph, *this);
        viewport.setViewedComponent(canvas.get(), false);
        viewport.setScrollBarsShown(true, true);
        addAndMakeVisible(viewport);

        for (auto* b : {&audioBtn, &scanBtn, &addBtn, &saveBtn, &loadBtn}) addAndMakeVisible(*b);
        audioBtn.onClick = [this] { showAudioSettings(); };
        scanBtn.onClick = [this] {
            juce::PopupMenu m;
            m.addItem(1, "Scan for new or changed plugins (fast)");
            m.addItem(2, "Rescan everything");
            m.addItem(3, "Add a plugin folder...");
            m.addSeparator();
            m.addItem(4, "Manage plugin list...");
            m.showMenuAsync(juce::PopupMenu::Options().withTargetComponent(&scanBtn), [this](int r) {
                if (r == 1) startScan(false);
                if (r == 2) startScan(true);
                if (r == 3) addFolder();
                if (r == 4) showPluginList();
            });
        };
        cancelBtn.onClick = [this] { scanStop = true; };
        addChildComponent(cancelBtn);
        addBtn.onClick = [this] { showAddMenu({200, 200}); };
        saveBtn.onClick = [this] { saveAs(); };
        loadBtn.onClick = [this] { openFile(); };
        status.setColour(juce::Label::textColourId, col::dim);
        status.setJustificationType(juce::Justification::centredRight);
        addAndMakeVisible(status);

        if (!loadSession(sessionFile())) buildDefault();
        setSize(1280, 800);
        startTimerHz(2);
    }

    ~Main() override {
        scanStop = true;
        scanPool.reset();
        saveSession(sessionFile());
        if (auto xml = deviceManager.createStateXml()) props.getUserSettings()->setValue("audioDevice", xml.get());
        props.saveIfNeeded();
        windows.clear();
        deviceManager.removeAudioCallback(&player);
        player.setProcessor(nullptr);
        graph.clear();
    }

    void paint(juce::Graphics& g) override {
        g.fillAll(col::bg);
        g.setColour(col::node);
        g.fillRect(getLocalBounds().removeFromTop(44));
        g.setColour(col::dfb);
        g.setFont(juce::FontOptions(18.f, juce::Font::bold));
        g.drawText("REA PLUGIN HUB", 14, 0, 200, 44, juce::Justification::centredLeft);
    }

    void resized() override {
        auto r = getLocalBounds();
        auto top = r.removeFromTop(44).reduced(8, 7);
        top.removeFromLeft(200);
        for (auto* b : {&audioBtn, &scanBtn, &addBtn, &saveBtn, &loadBtn}) { b->setBounds(top.removeFromLeft(b == &scanBtn ? 130 : 110)); top.removeFromLeft(6); }
        if (cancelBtn.isVisible()) { cancelBtn.setBounds(top.removeFromRight(80)); top.removeFromRight(6); }
        status.setBounds(top);
        viewport.setBounds(r);
    }

    // ---- Canvas::Host
    void showAddMenu(juce::Point<int> where) override {
        juce::PopupMenu m;
        m.addItem(1, "De-Feedback AI (built in)");
        m.addSeparator();
        juce::PopupMenu plugs;
        auto types = knownPlugins.getTypes();
        std::sort(types.begin(), types.end(), [](auto& a, auto& b) { return a.manufacturerName + a.name < b.manufacturerName + b.name; });
        std::map<juce::String, juce::PopupMenu> byMaker;
        for (int i = 0; i < types.size(); ++i) byMaker[types[i].manufacturerName].addItem(100 + i, types[i].name);
        for (auto& [maker, sub] : byMaker) plugs.addSubMenu(maker.isEmpty() ? "(unknown)" : maker, sub);
        if (types.isEmpty()) plugs.addItem(2, "No plugins yet: scan first", false);
        m.addSubMenu("VST3 plugins", plugs);
        m.addSeparator();
        m.addItem(3, "Scan for plugins...");
        m.showMenuAsync({}, [this, where, types](int r) {
            if (r == 1) addInternal(where);
            else if (r == 3) showPluginList();
            else if (r >= 100 && r - 100 < types.size()) addPlugin(types[r - 100], where);
        });
    }

    void openWindow(NodeID id) override {
        for (auto& w : windows) if (w->node->nodeID == id) { w->toFront(true); return; }
        if (auto n = graph.getNodeForId(id)) {
            if (dynamic_cast<IOProc*>(n->getProcessor())) return;
            windows.add(new PluginWindow(n, [this](NodeID nid) { closeWindow(nid); }));
        }
    }

    void deleteNode(NodeID id) override {
        closeWindow(id);
        graph.removeNode(id);
    }

    juce::String channelName(bool input, int ch) override {
        if (auto* dev = deviceManager.getCurrentAudioDevice()) {
            auto names = input ? dev->getInputChannelNames() : dev->getOutputChannelNames();
            auto active = input ? dev->getActiveInputChannels() : dev->getActiveOutputChannels();
            int k = -1;
            for (int i = 0; i < names.size(); ++i) if (active[i] && ++k == ch) return names[i];
        }
        return juce::String(ch + 1);
    }

private:
    juce::File appDir() const {
        auto d = juce::File::getSpecialLocation(juce::File::userApplicationDataDirectory).getChildFile("REA Plugin Hub");
        d.createDirectory();
        return d;
    }
    juce::File sessionFile() const { return appDir().getChildFile("last-session.reahub"); }

    void closeWindow(NodeID id) {
        for (int i = windows.size(); --i >= 0;) if (windows[i]->node->nodeID == id) windows.remove(i);
    }

    Graph::Node::Ptr addInternal(juce::Point<int> at) {
        auto n = graph.addNode(std::make_unique<DeFeedbackProcessor>());
        if (n) { n->properties.set("x", at.x); n->properties.set("y", at.y); n->properties.set("type", kInternalDfb); }
        return n;
    }

    Graph::Node::Ptr addPlugin(const juce::PluginDescription& d, juce::Point<int> at) {
        juce::String err;
        auto inst = formats.createPluginInstance(d, graph.getSampleRate() > 0 ? graph.getSampleRate() : 48000.0,
                                                 graph.getBlockSize() > 0 ? graph.getBlockSize() : 256, err);
        if (!inst) {
            juce::AlertWindow::showMessageBoxAsync(juce::MessageBoxIconType::WarningIcon, "Couldn't load plugin", d.name + "\n" + err);
            return nullptr;
        }
        auto n = graph.addNode(std::move(inst));
        if (n) {
            n->properties.set("x", at.x); n->properties.set("y", at.y);
            n->properties.set("type", "plugin");
            if (auto xml = d.createXml()) n->properties.set("desc", xml->toString());
        }
        return n;
    }

    void buildDefault() {
        graph.clear();
        auto in = graph.addNode(std::make_unique<IOProc>(IOProc::audioInputNode));
        auto out = graph.addNode(std::make_unique<IOProc>(IOProc::audioOutputNode));
        in->properties.set("x", 40); in->properties.set("y", 60);
        out->properties.set("x", 760); out->properties.set("y", 60);
        auto dfb = addInternal({400, 80});
        // wiring follows once the device sets the channel counts
        juce::MessageManager::callAsync([this, i = in->nodeID, o = out->nodeID, d = dfb->nodeID] {
            graph.addConnection({{i, 0}, {d, 0}});
            graph.addConnection({{i, 0}, {d, 1}});
            graph.addConnection({{d, 0}, {o, 0}});
            graph.addConnection({{d, 1}, {o, 1}});
        });
    }

    // ---- sessions
    std::unique_ptr<juce::XmlElement> sessionXml() {
        auto root = std::make_unique<juce::XmlElement>("REAHUB");
        for (auto* n : graph.getNodes()) {
            auto* e = root->createNewChildElement("NODE");
            e->setAttribute("uid", (int)n->nodeID.uid);
            e->setAttribute("x", (int)n->properties.getWithDefault("x", 100));
            e->setAttribute("y", (int)n->properties.getWithDefault("y", 100));
            e->setAttribute("bypass", n->isBypassed());
            if (auto* io = dynamic_cast<IOProc*>(n->getProcessor())) { e->setAttribute("type", io->isInput() ? "in" : "out"); continue; }
            e->setAttribute("type", n->properties.getWithDefault("type", "plugin").toString());
            if (n->properties.contains("desc")) e->setAttribute("desc", n->properties["desc"].toString());
            juce::MemoryBlock mb;
            n->getProcessor()->getStateInformation(mb);
            e->setAttribute("state", mb.toBase64Encoding());
        }
        for (auto& c : graph.getConnections()) {
            if (c.source.isMIDI()) continue;
            auto* e = root->createNewChildElement("CONN");
            e->setAttribute("src", (int)c.source.nodeID.uid); e->setAttribute("srcCh", c.source.channelIndex);
            e->setAttribute("dst", (int)c.destination.nodeID.uid); e->setAttribute("dstCh", c.destination.channelIndex);
        }
        return root;
    }

    void saveSession(const juce::File& f) { if (auto x = sessionXml()) x->writeTo(f); }

    bool loadSession(const juce::File& f) {
        auto root = juce::XmlDocument::parse(f);
        if (!root || !root->hasTagName("REAHUB")) return false;
        windows.clear();
        graph.clear();
        std::map<int, NodeID> ids;
        juce::StringArray missing;
        for (auto* e : root->getChildWithTagNameIterator("NODE")) {
            const auto type = e->getStringAttribute("type");
            const juce::Point<int> at{e->getIntAttribute("x"), e->getIntAttribute("y")};
            Graph::Node::Ptr n;
            if (type == "in" || type == "out") {
                n = graph.addNode(std::make_unique<IOProc>(type == "in" ? IOProc::audioInputNode : IOProc::audioOutputNode));
                n->properties.set("x", at.x); n->properties.set("y", at.y);
            } else if (type == kInternalDfb) {
                n = addInternal(at);
            } else if (auto dx = juce::parseXML(e->getStringAttribute("desc"))) {
                juce::PluginDescription d;
                if (d.loadFromXml(*dx)) { n = addPlugin(d, at); if (!n) missing.add(d.name); }
            }
            if (!n) continue;
            juce::MemoryBlock mb;
            if (mb.fromBase64Encoding(e->getStringAttribute("state")) && mb.getSize() > 0)
                n->getProcessor()->setStateInformation(mb.getData(), (int)mb.getSize());
            n->setBypassed(e->getBoolAttribute("bypass"));
            ids[e->getIntAttribute("uid")] = n->nodeID;
        }
        std::vector<Graph::Connection> conns;
        for (auto* e : root->getChildWithTagNameIterator("CONN")) {
            auto s = ids.find(e->getIntAttribute("src")), d = ids.find(e->getIntAttribute("dst"));
            if (s != ids.end() && d != ids.end())
                conns.push_back({{s->second, e->getIntAttribute("srcCh")}, {d->second, e->getIntAttribute("dstCh")}});
        }
        juce::MessageManager::callAsync([this, conns] { for (auto& c : conns) graph.addConnection(c); });
        if (!missing.isEmpty())
            juce::AlertWindow::showMessageBoxAsync(juce::MessageBoxIconType::WarningIcon, "Some plugins are missing", missing.joinIntoString("\n"));
        return true;
    }

    void saveAs() {
        chooser = std::make_unique<juce::FileChooser>("Save routing", appDir(), "*.reahub");
        chooser->launchAsync(juce::FileBrowserComponent::saveMode | juce::FileBrowserComponent::warnAboutOverwriting, [this](const juce::FileChooser& fc) {
            auto f = fc.getResult();
            if (f != juce::File{}) saveSession(f.withFileExtension("reahub"));
        });
    }

    void openFile() {
        chooser = std::make_unique<juce::FileChooser>("Open routing", appDir(), "*.reahub");
        chooser->launchAsync(juce::FileBrowserComponent::openMode | juce::FileBrowserComponent::canSelectFiles, [this](const juce::FileChooser& fc) {
            auto f = fc.getResult();
            if (f.existsAsFile()) loadSession(f);
        });
    }

    // ---- fast plugin scan: background, parallel, skips plugins whose files haven't
    // changed, and JUCE reads moduleinfo.json where a plugin ships one (no loading).
    juce::AudioPluginFormat* vst3Format() {
        for (int i = 0; i < formats.getNumFormats(); ++i)
            if (formats.getFormat(i)->getName() == "VST3") return formats.getFormat(i);
        return nullptr;
    }

    juce::FileSearchPath scanPath() {
        auto* f = vst3Format();
        juce::FileSearchPath p = f ? f->getDefaultLocationsToSearch() : juce::FileSearchPath();
        juce::FileSearchPath extra(props.getUserSettings()->getValue("extraVst3Paths"));
        for (int i = 0; i < extra.getNumPaths(); ++i) p.add(extra.getRawString(i));
        p.removeRedundantPaths();
        p.removeNonExistentPaths();
        return p;
    }

    void addFolder() {
        chooser = std::make_unique<juce::FileChooser>("Add a VST3 folder", juce::File("C:\\Program Files\\Common Files\\VST3"));
        chooser->launchAsync(juce::FileBrowserComponent::openMode | juce::FileBrowserComponent::canSelectDirectories, [this](const juce::FileChooser& fc) {
            auto d = fc.getResult();
            if (!d.isDirectory()) return;
            juce::FileSearchPath extra(props.getUserSettings()->getValue("extraVst3Paths"));
            extra.addIfNotAlreadyThere(d);
            props.getUserSettings()->setValue("extraVst3Paths", extra.toString());
            props.saveIfNeeded();
            startScan(false);
        });
    }

    void startScan(bool everything) {
        if (scanPool) return;
        auto* fmt = vst3Format();
        if (!fmt) return;
        scanStop = false;
        scanFound = 0;
        const int before = knownPlugins.getNumTypes();
        scanBefore = before;
        scanEverything = everything;
        scanner = std::make_unique<juce::PluginDirectoryScanner>(knownPlugins, *fmt, scanPath(), true,
                                                                 appDir().getChildFile("scan-crash-guard.txt"), true);
        scanTotal = fmt->searchPathsForPlugins(scanPath(), true, true).size();
        const int threads = juce::jlimit(2, 8, juce::SystemStats::getNumCpus());
        scanPool = std::make_unique<juce::ThreadPool>(juce::ThreadPoolOptions{}.withNumberOfThreads(threads));
        scanJobsLeft = threads;
        for (int i = 0; i < threads; ++i)
            scanPool->addJob([this] {
                juce::String name;
                while (!scanStop.load()) {
                    { const juce::ScopedLock sl(scanNameLock); scanCurrent = scanner->getNextPluginFileThatWillBeScanned(); }
                    if (!scanner->scanNextFile(!scanEverything, name)) break;
                }
                if (--scanJobsLeft == 0) juce::MessageManager::callAsync([this] { finishScan(); });
            });
        cancelBtn.setVisible(true);
        resized();
        startTimerHz(8);
    }

    void finishScan() {
        if (!scanPool) return;
        scanPool.reset();
        const auto failed = scanner->getFailedFiles();
        scanner.reset();
        cancelBtn.setVisible(false);
        resized();
        startTimerHz(2);
        juce::String msg;
        msg << (scanStop ? "Scan cancelled. " : "Scan finished. ") << knownPlugins.getNumTypes() << " plugins known ("
            << juce::jmax(0, knownPlugins.getNumTypes() - scanBefore) << " new).";
        if (!failed.isEmpty()) msg << "  " << failed.size() << " couldn't be loaded (see Manage plugin list).";
        scanResult = msg;
        if (quitAfterScan) {
            std::printf("%s\n", msg.toRawUTF8());
            for (auto& t : knownPlugins.getTypes()) std::printf("  %s | %s\n", t.manufacturerName.toRawUTF8(), t.name.toRawUTF8());
            std::fflush(stdout);
            juce::JUCEApplication::getInstance()->systemRequestedQuit();
        }
        scanResultUntil = juce::Time::getMillisecondCounterHiRes() + 15000;
    }

    // ---- dialogs
    void showAudioSettings() {
        auto* sel = new juce::AudioDeviceSelectorComponent(deviceManager, 0, 256, 0, 256, false, false, true, false);
        sel->setSize(560, 520);
        juce::DialogWindow::LaunchOptions o;
        o.content.setOwned(sel);
        o.dialogTitle = "Audio device (choose ASIO and your desk)";
        o.dialogBackgroundColour = col::node;
        o.useNativeTitleBar = true;
        o.resizable = true;
        o.launchAsync();
    }

    void showPluginList() {
        auto* list = new juce::PluginListComponent(formats, knownPlugins, appDir().getChildFile("scan-crash-guard.txt"), props.getUserSettings(), true);
        list->setNumberOfThreadsForScanning(juce::jlimit(2, 8, juce::SystemStats::getNumCpus()));
        list->setSize(720, 520);
        juce::DialogWindow::LaunchOptions o;
        o.content.setOwned(list);
        o.dialogTitle = "VST3 plugins: Options > Scan for new or updated VST3 plug-ins";
        o.dialogBackgroundColour = col::node;
        o.useNativeTitleBar = true;
        o.resizable = true;
        o.launchAsync();
    }

    void changeListenerCallback(juce::ChangeBroadcaster* src) override {
        if (src == &knownPlugins) {
            if (auto xml = knownPlugins.createXml()) { props.getUserSettings()->setValue("pluginList", xml.get()); props.saveIfNeeded(); }
        } else if (src == &deviceManager) {
            if (auto xml = deviceManager.createStateXml()) { props.getUserSettings()->setValue("audioDevice", xml.get()); props.saveIfNeeded(); }
            canvas->repaint();
        }
    }

    void timerCallback() override {
        if (scanPool && scanner) {
            juce::String cur;
            { const juce::ScopedLock sl(scanNameLock); cur = scanCurrent; }
            const int pct = juce::roundToInt(scanner->getProgress() * 100.f);
            status.setText("Scanning VST3 " + juce::String(pct) + "% of " + juce::String(scanTotal) + " files  |  "
                           + juce::String(knownPlugins.getNumTypes() - scanBefore) + " new  |  " + cur, juce::dontSendNotification);
            return;
        }
        if (juce::Time::getMillisecondCounterHiRes() < scanResultUntil) { status.setText(scanResult, juce::dontSendNotification); return; }
        juce::String s;
        if (auto* d = deviceManager.getCurrentAudioDevice())
            s << d->getTypeName() << ": " << d->getName() << "  |  " << juce::String(d->getCurrentSampleRate() / 1000.0, 1) << " kHz, "
              << d->getCurrentBufferSizeSamples() << " samples  |  CPU " << juce::String(deviceManager.getCpuUsage() * 100.0, 0)
              << "%  |  dropouts " << d->getXRunCount();
        else s = "No audio device: click Audio settings";
        status.setText(s, juce::dontSendNotification);
    }

    juce::ApplicationProperties props;
    juce::AudioDeviceManager deviceManager;
    juce::AudioPluginFormatManager formats;
    juce::KnownPluginList knownPlugins;
    Graph graph;
    juce::AudioProcessorPlayer player;
    juce::Viewport viewport;
    std::unique_ptr<Canvas> canvas;
    juce::TextButton audioBtn{"Audio settings"}, scanBtn{"Plugins..."}, addBtn{"Add node"}, saveBtn{"Save..."}, loadBtn{"Open..."};
    juce::Label status;
    juce::TextButton cancelBtn{"Cancel"};
    std::unique_ptr<juce::PluginDirectoryScanner> scanner;
    std::unique_ptr<juce::ThreadPool> scanPool;
    std::atomic<bool> scanStop{false};
    std::atomic<int> scanJobsLeft{0}, scanFound{0};
    bool scanEverything = false;
    int scanBefore = 0, scanTotal = 0;
    juce::CriticalSection scanNameLock;
    juce::String scanCurrent, scanResult;
    double scanResultUntil = 0;
public:
    bool quitAfterScan = false;
    void scanAndQuit() { quitAfterScan = true; startScan(true); }
private:
    juce::OwnedArray<PluginWindow> windows;
    std::unique_ptr<juce::FileChooser> chooser;
};

class Window : public juce::DocumentWindow {
public:
    explicit Window(bool scanTest) : DocumentWindow("REA Plugin Hub", col::bg, DocumentWindow::allButtons) {
        setUsingNativeTitleBar(true);
        auto* m = new Main();
        setContentOwned(m, true);
        if (scanTest) juce::MessageManager::callAsync([m] { m->scanAndQuit(); });
        setResizable(true, false);
        centreWithSize(getWidth(), getHeight());
        setVisible(true);
    }
    void closeButtonPressed() override { juce::JUCEApplication::getInstance()->systemRequestedQuit(); }
};

class App : public juce::JUCEApplication {
public:
    const juce::String getApplicationName() override { return "REA Plugin Hub"; }
    const juce::String getApplicationVersion() override { return "1.0.0"; }
    bool moreThanOneInstanceAllowed() override { return false; }
    void initialise(const juce::String& cmd) override {
        juce::LookAndFeel::getDefaultLookAndFeel().setColour(juce::ResizableWindow::backgroundColourId, col::bg);
        window = std::make_unique<Window>(cmd.contains("--scan-and-quit"));
    }
    void shutdown() override { window.reset(); }
    void systemRequestedQuit() override { quit(); }
private:
    std::unique_ptr<Window> window;
};

} // namespace hub

START_JUCE_APPLICATION(hub::App)
