// expect: line 7: image 'never-built' is not built
pipeline {
    agent any
    stages {
        stage('Deploy') {
            steps {
                deploy service: 'app', image: 'never-built'
            }
        }
    }
}
