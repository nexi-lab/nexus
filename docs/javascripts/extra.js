// Nexus Documentation - Custom JavaScript
// Interactive features and enhanced UX

document.addEventListener('DOMContentLoaded', function() {
  console.log('🚀 Nexus Documentation loaded');

  // ==========================================
  // Animated Counter for Stats
  // ==========================================
  function animateCounter(element, target, duration = 2000) {
    const start = 0;
    const increment = target / (duration / 16); // 60fps
    let current = start;

    const timer = setInterval(() => {
      current += increment;
      if (current >= target) {
        element.textContent = formatNumber(target);
        clearInterval(timer);
      } else {
        element.textContent = formatNumber(Math.floor(current));
      }
    }, 16);
  }

  function formatNumber(num) {
    if (num >= 1000) {
      return (num / 1000).toFixed(1) + 'K';
    }
    return num.toString();
  }

  // Animate stats when they come into view
  const observerOptions = {
    threshold: 0.5,
    rootMargin: '0px'
  };

  const statsObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const h3 = entry.target.querySelector('h3');
        if (h3 && !h3.dataset.animated) {
          const text = h3.textContent.trim();
          const match = text.match(/(\d+\.?\d*)([KkMm%+]*)/);
          if (match) {
            const num = parseFloat(match[1]);
            const suffix = match[2];
            const multiplier = suffix.toLowerCase().includes('k') ? 1000 : 1;
            const target = num * multiplier;

            h3.dataset.animated = 'true';
            h3.textContent = '0';

            setTimeout(() => {
              let current = 0;
              const increment = target / 100;
              const timer = setInterval(() => {
                current += increment;
                if (current >= target) {
                  h3.textContent = match[1] + suffix;
                  clearInterval(timer);
                } else {
                  const display = suffix.toLowerCase().includes('k')
                    ? (current / 1000).toFixed(1) + suffix
                    : Math.floor(current) + suffix;
                  h3.textContent = display;
                }
              }, 20);
            }, 200);
          }
        }
        statsObserver.unobserve(entry.target);
      }
    });
  }, observerOptions);

  // Observe all stat cards
  document.querySelectorAll('.stat-card').forEach(card => {
    statsObserver.observe(card);
  });

  // ==========================================
  // Smooth Scroll for Anchor Links
  // ==========================================
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function(e) {
      const href = this.getAttribute('href');
      if (href !== '#') {
        e.preventDefault();
        const target = document.querySelector(href);
        if (target) {
          target.scrollIntoView({
            behavior: 'smooth',
            block: 'start'
          });
        }
      }
    });
  });

  // ==========================================
  // Feature Card Animation on Scroll
  // ==========================================
  const cardObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.style.opacity = '0';
        entry.target.style.transform = 'translateY(20px)';

        setTimeout(() => {
          entry.target.style.transition = 'opacity 0.6s ease, transform 0.6s ease';
          entry.target.style.opacity = '1';
          entry.target.style.transform = 'translateY(0)';
        }, 100);

        cardObserver.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1 });

  document.querySelectorAll('.feature-card, .value-prop, .example-card').forEach(card => {
    cardObserver.observe(card);
  });

  // ==========================================
  // Copy Code Button Enhancement
  // ==========================================
  document.querySelectorAll('pre > code').forEach((codeBlock) => {
    const pre = codeBlock.parentElement;

    // Create copy button if it doesn't exist
    if (!pre.querySelector('.copy-button')) {
      const button = document.createElement('button');
      button.className = 'copy-button';
      button.innerHTML = `
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
        </svg>
      `;
      button.title = 'Copy code';

      button.addEventListener('click', async () => {
        const code = codeBlock.textContent;
        try {
          await navigator.clipboard.writeText(code);
          button.innerHTML = `
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="20 6 9 17 4 12"></polyline>
            </svg>
          `;
          button.style.color = '#2e7d32';

          setTimeout(() => {
            button.innerHTML = `
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
              </svg>
            `;
            button.style.color = '';
          }, 2000);
        } catch (err) {
          console.error('Failed to copy:', err);
        }
      });

      pre.style.position = 'relative';
      pre.appendChild(button);
    }
  });

  // ==========================================
  // Add Table of Contents Highlighting
  // ==========================================
  const headings = document.querySelectorAll('h2[id], h3[id]');
  const tocLinks = document.querySelectorAll('.md-nav--secondary a');

  const headingObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const id = entry.target.getAttribute('id');
        tocLinks.forEach(link => {
          if (link.getAttribute('href') === `#${id}`) {
            link.classList.add('active-toc');
          } else {
            link.classList.remove('active-toc');
          }
        });
      }
    });
  }, { threshold: 0.5 });

  headings.forEach(heading => {
    headingObserver.observe(heading);
  });

  // ==========================================
  // Add Interactive API Examples
  // ==========================================
  document.querySelectorAll('.tabbed-set').forEach(tabbedSet => {
    const labels = tabbedSet.querySelectorAll('.tabbed-labels label');
    labels.forEach((label, index) => {
      label.style.transition = 'all 0.3s ease';
      label.addEventListener('mouseenter', () => {
        if (!label.querySelector('input').checked) {
          label.style.backgroundColor = 'rgba(94, 53, 177, 0.05)';
        }
      });
      label.addEventListener('mouseleave', () => {
        if (!label.querySelector('input').checked) {
          label.style.backgroundColor = '';
        }
      });
    });
  });

  // ==========================================
  // Add Search Enhancement
  // ==========================================
  const searchInput = document.querySelector('.md-search__input');
  if (searchInput) {
    searchInput.addEventListener('focus', () => {
      searchInput.parentElement.style.boxShadow = '0 4px 12px rgba(94, 53, 177, 0.2)';
    });
    searchInput.addEventListener('blur', () => {
      searchInput.parentElement.style.boxShadow = '';
    });
  }

  // ==========================================
  // Add Dark Mode Enhancements
  // ==========================================
  const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      if (mutation.attributeName === 'data-md-color-scheme') {
        const isDark = document.body.getAttribute('data-md-color-scheme') === 'slate';
        document.documentElement.style.setProperty(
          '--gradient-hero',
          isDark
            ? 'linear-gradient(135deg, #4527a0 0%, #5e35b1 50%, #7e57c2 100%)'
            : 'linear-gradient(135deg, #5e35b1 0%, #7e57c2 50%, #9575cd 100%)'
        );
      }
    });
  });

  observer.observe(document.body, {
    attributes: true,
    attributeFilter: ['data-md-color-scheme']
  });

  // ==========================================
  // Add Scroll Progress Indicator
  // ==========================================
  const progressBar = document.createElement('div');
  progressBar.style.cssText = `
    position: fixed;
    top: 0;
    left: 0;
    height: 3px;
    background: linear-gradient(90deg, #5e35b1 0%, #7e57c2 100%);
    z-index: 9999;
    transition: width 0.1s ease;
    width: 0%;
  `;
  document.body.appendChild(progressBar);

  window.addEventListener('scroll', () => {
    const windowHeight = document.documentElement.scrollHeight - document.documentElement.clientHeight;
    const scrolled = (window.scrollY / windowHeight) * 100;
    progressBar.style.width = scrolled + '%';
  });

  // ==========================================
  // Add Keyboard Shortcuts
  // ==========================================
  document.addEventListener('keydown', (e) => {
    // Ctrl/Cmd + K to focus search
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      const searchInput = document.querySelector('.md-search__input');
      if (searchInput) {
        searchInput.focus();
      }
    }
  });

  // ==========================================
  // Add Copy Button Styles
  // ==========================================
  const style = document.createElement('style');
  style.textContent = `
    .copy-button {
      position: absolute;
      top: 0.5rem;
      right: 0.5rem;
      padding: 0.5rem;
      background: rgba(255, 255, 255, 0.9);
      border: 1px solid #e0e0e0;
      border-radius: 4px;
      cursor: pointer;
      opacity: 0;
      transition: all 0.2s ease;
      z-index: 10;
    }

    .copy-button:hover {
      background: white;
      box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }

    pre:hover .copy-button {
      opacity: 1;
    }

    .copy-button svg {
      width: 16px;
      height: 16px;
      display: block;
    }

    .active-toc {
      color: var(--nexus-primary) !important;
      font-weight: 600 !important;
      border-left: 2px solid var(--nexus-primary) !important;
    }

    @media (prefers-color-scheme: dark) {
      .copy-button {
        background: rgba(30, 30, 30, 0.9);
        border-color: #404040;
        color: white;
      }
    }
  `;
  document.head.appendChild(style);

  // ==========================================
  // Add Version Badge Animation
  // ==========================================
  const versionBadges = document.querySelectorAll('[class*="version"]');
  versionBadges.forEach(badge => {
    badge.style.transition = 'transform 0.3s ease';
    badge.addEventListener('mouseenter', () => {
      badge.style.transform = 'scale(1.05)';
    });
    badge.addEventListener('mouseleave', () => {
      badge.style.transform = 'scale(1)';
    });
  });

  console.log('✨ All interactive features loaded');
});

// ==========================================
// External Link Icon
// ==========================================
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('a[href^="http"]').forEach(link => {
    if (!link.hostname.includes('nexi-lab.github.io') &&
        !link.hostname.includes('localhost')) {
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener noreferrer');

      // Add external link icon
      const icon = document.createElement('span');
      icon.innerHTML = ' ↗';
      icon.style.fontSize = '0.8em';
      icon.style.opacity = '0.6';
      link.appendChild(icon);
    }
  });
});
