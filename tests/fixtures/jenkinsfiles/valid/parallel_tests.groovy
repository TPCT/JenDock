pipeline {
    agent { docker { image 'python:3.13' } }
    stages {
        stage('Prepare') {
            steps { sh 'pip install -r requirements.txt' }
        }
        stage('Tests') {
            parallel {
                stage('Unit') {
                    steps { sh 'pytest tests/unit' }
                }
                stage('Integration') {
                    agent { docker { image 'python:3.13'; args '-e DB_HOST=db' } }
                    steps { sh 'pytest tests/integration' }
                }
                stage('Slow') {
                    stages {
                        stage('Fixtures') {
                            steps { sh 'make fixtures' }
                        }
                        stage('Run') {
                            steps { sh 'pytest tests/slow' }
                        }
                    }
                }
            }
        }
    }
}
