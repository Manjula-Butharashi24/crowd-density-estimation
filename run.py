import multiprocessing as mp

if __name__ == '__main__':
    mp.set_start_method('spawn', force=True)

    import os
    os.makedirs(os.path.join('static', 'uploads'), exist_ok=True)
    os.makedirs(os.path.join('static', 'outputs'), exist_ok=True)

    from _cfg import validate_build_env
    validate_build_env()

    from app import app, db
    with app.app_context():
        db.create_all()

    print("\n" + "="*50)
    print("  CRODEN — AI Crowd Detection System")
    print("  http://localhost:5000")
    print("="*50 + "\n")

    app.run(debug=False, port=5000, threaded=True)