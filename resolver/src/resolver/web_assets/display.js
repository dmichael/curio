(() => {
  const body = document.body;
  const stage = document.getElementById('stage');
  const uri = body.dataset.mediaUri;
  const fail = (message) => {
    stage.replaceChildren();
    const box = document.createElement('div');
    box.className = 'display-error';
    box.textContent = message;
    stage.append(box);
  };
  const mainType = (value) => (value || '').split(';', 1)[0].trim().toLowerCase();
  const render = (type) => {
    let node;
    if (type.startsWith('image/')) {
      node = document.createElement('img');
      node.alt = '';
    } else if (type.startsWith('video/')) {
      node = document.createElement('video');
      node.autoplay = true;
      node.loop = true;
      node.controls = true;
      node.playsInline = true;
      node.muted = true;
    } else if (type.startsWith('audio/')) {
      node = document.createElement('audio');
      node.autoplay = true;
      node.controls = true;
    } else if (type === 'text/html' || type === 'application/xhtml+xml') {
      node = document.createElement('iframe');
      node.title = 'Artwork';
      node.setAttribute('sandbox', 'allow-scripts');
    } else {
      fail(`Curio cannot display media of type ${type || 'unknown'}.`);
      return;
    }
    node.addEventListener('error', () => fail('The artwork could not be loaded.'));
    node.src = uri;
    stage.replaceChildren(node);
    if (node instanceof HTMLMediaElement) {
      const attempt = node.play();
      if (attempt) attempt.catch(() => { node.controls = true; });
    }
  };
  fetch(uri, { method: 'HEAD', credentials: 'same-origin' })
    .then((response) => {
      if (!response.ok) throw new Error(`Media request returned ${response.status}.`);
      render(mainType(response.headers.get('content-type')));
    })
    .catch((error) => fail(error.message || 'The artwork could not be inspected.'));
})();
